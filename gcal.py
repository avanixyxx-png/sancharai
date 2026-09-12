"""
Google Calendar sync.

Two directions, and the second is the one that matters:

  PUSH - an appointment booked on a call appears in the customer's Google
         Calendar, so their staff see it where they already look.

  PULL - before confirming a slot, Google is asked whether that time is busy.
         Without this the two diaries drift: someone blocks 11am in Google,
         the agent knows nothing about it, and books over the top. Push-only
         sync looks fine in a demo and double-books in real use.

Deliberately plain HTTP rather than google-api-python-client: that SDK pulls in
a large dependency tree for what is three REST calls, and this has to run on a
free-tier host.

Tokens are per-company and encrypted at rest, same as Twilio and AI keys.
Google's refresh token is long-lived, so leaking one is as serious as leaking
a password - it is never logged or returned to the browser.
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

import httpx

from security import decrypt, encrypt

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
API = "https://www.googleapis.com/calendar/v3"

# calendar.events is the narrowest scope that allows both creating events and
# reading free/busy. Asking for full calendar access would be more than needed
# and makes the consent screen scarier than it has to be.
SCOPE = "https://www.googleapis.com/auth/calendar.events"


class CalendarError(Exception):
    """A Google Calendar problem worth surfacing, never fatal to a call."""


def configured() -> bool:
    """Whether the PLATFORM has Google OAuth set up at all."""
    return bool(
        os.getenv("GOOGLE_CLIENT_ID", "").strip()
        and os.getenv("GOOGLE_CLIENT_SECRET", "").strip()
    )


def connected(settings) -> bool:
    """Whether THIS company has linked their calendar."""
    return bool(getattr(settings, "google_refresh_token_enc", ""))


def consent_url(redirect_uri: str, state: str) -> str:
    """Where to send the user to grant access."""
    from urllib.parse import urlencode

    return AUTH_URL + "?" + urlencode({
        "client_id": os.getenv("GOOGLE_CLIENT_ID", "").strip(),
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": SCOPE,
        # offline + consent is what actually returns a refresh token. Without
        # both, Google hands back only a 1-hour access token and the
        # integration silently stops working after the first hour.
        "access_type": "offline",
        "prompt": "consent",
        "state": state,
        "include_granted_scopes": "true",
    })


def exchange_code(code: str, redirect_uri: str) -> str:
    """Swap the one-time code for a long-lived refresh token."""
    try:
        r = httpx.post(TOKEN_URL, data={
            "code": code,
            "client_id": os.getenv("GOOGLE_CLIENT_ID", "").strip(),
            "client_secret": os.getenv("GOOGLE_CLIENT_SECRET", "").strip(),
            "redirect_uri": redirect_uri,
            "grant_type": "authorization_code",
        }, timeout=15)
        r.raise_for_status()
    except httpx.HTTPError as exc:
        raise CalendarError(f"Google rejected the connection: {exc}") from exc

    token = r.json().get("refresh_token", "")
    if not token:
        raise CalendarError(
            "Google did not return a refresh token. Remove SancharAI from your "
            "Google account's third-party access list and connect again."
        )
    return token


def _access_token(settings) -> str:
    """Trade the stored refresh token for a short-lived access token."""
    refresh = decrypt(getattr(settings, "google_refresh_token_enc", ""))
    if not refresh:
        raise CalendarError("Google Calendar is not connected.")
    try:
        r = httpx.post(TOKEN_URL, data={
            "refresh_token": refresh,
            "client_id": os.getenv("GOOGLE_CLIENT_ID", "").strip(),
            "client_secret": os.getenv("GOOGLE_CLIENT_SECRET", "").strip(),
            "grant_type": "refresh_token",
        }, timeout=15)
        r.raise_for_status()
    except httpx.HTTPError as exc:
        raise CalendarError(f"Could not refresh Google access: {exc}") from exc
    return r.json().get("access_token", "")


def _calendar_id(settings) -> str:
    return getattr(settings, "google_calendar_id", "") or "primary"


def is_busy(settings, start_utc: datetime, end_utc: datetime) -> bool:
    """Does Google think this window is busy?

    Returns False on ANY failure. A calendar outage must not stop the business
    taking bookings - the local diary is still authoritative, and refusing every
    slot because Google is unreachable would be worse than an occasional
    double-booking.
    """
    if not connected(settings):
        return False
    try:
        token = _access_token(settings)
        r = httpx.post(
            f"{API}/freeBusy",
            headers={"Authorization": f"Bearer {token}"},
            json={
                "timeMin": start_utc.replace(tzinfo=timezone.utc).isoformat(),
                "timeMax": end_utc.replace(tzinfo=timezone.utc).isoformat(),
                "items": [{"id": _calendar_id(settings)}],
            },
            timeout=8,
        )
        r.raise_for_status()
        cals = r.json().get("calendars", {})
        for cal in cals.values():
            if cal.get("busy"):
                return True
    except Exception as exc:
        print(f"[gcal] free/busy check failed, allowing booking: {exc}", flush=True)
    return False


def create_event(settings, *, summary: str, description: str,
                 start_utc: datetime, end_utc: datetime, tz_name: str) -> str:
    """Push an appointment to Google. Returns the event id, or "" on failure.

    Never raises: a booking that succeeded locally must not be rolled back
    because Google was slow. The appointment is real either way; only the
    mirror copy is missing, and that is recoverable.
    """
    if not connected(settings):
        return ""
    try:
        token = _access_token(settings)
        r = httpx.post(
            f"{API}/calendars/{_calendar_id(settings)}/events",
            headers={"Authorization": f"Bearer {token}"},
            json={
                "summary": summary,
                "description": description,
                "start": {"dateTime": start_utc.replace(tzinfo=timezone.utc).isoformat(),
                          "timeZone": tz_name},
                "end": {"dateTime": end_utc.replace(tzinfo=timezone.utc).isoformat(),
                        "timeZone": tz_name},
            },
            timeout=10,
        )
        r.raise_for_status()
        return r.json().get("id", "")
    except Exception as exc:
        print(f"[gcal] could not create event: {exc}", flush=True)
        return ""


def delete_event(settings, event_id: str) -> bool:
    """Remove a cancelled appointment from Google. Best effort."""
    if not (connected(settings) and event_id):
        return False
    try:
        token = _access_token(settings)
        r = httpx.delete(
            f"{API}/calendars/{_calendar_id(settings)}/events/{event_id}",
            headers={"Authorization": f"Bearer {token}"},
            timeout=10,
        )
        # 410 means it is already gone, which is the outcome we wanted anyway.
        return r.status_code in (200, 204, 404, 410)
    except Exception as exc:
        print(f"[gcal] could not delete event: {exc}", flush=True)
        return False


def test_connection(settings) -> tuple[bool, str]:
    """Prove the link works, from Settings rather than mid-call."""
    if not configured():
        return False, "Google OAuth is not configured on this server."
    if not connected(settings):
        return False, "Not connected yet."
    try:
        token = _access_token(settings)
        r = httpx.get(
            f"{API}/calendars/{_calendar_id(settings)}",
            headers={"Authorization": f"Bearer {token}"},
            timeout=10,
        )
        r.raise_for_status()
        return True, f"Connected to '{r.json().get('summary', 'your calendar')}'"
    except Exception as exc:
        return False, f"Google rejected the request: {exc}"

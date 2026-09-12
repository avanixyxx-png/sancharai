"""
Appointment booking during a call.

The hard part is not storing an appointment - it is that the agent has already
said "yes, booked for eleven" by the time we discover eleven is taken. So the
flow deliberately splits into two steps:

  1. The model proposes a time alongside its reply.
  2. THIS module decides whether that time is actually free, and if it is not,
     replaces the model's reply with a deterministic one offering the next
     free slot.

Step 2 never calls the model again. A second round-trip mid-call would add a
second of dead air, and the alternative slot is arithmetic, not judgement.

Every stored time is naive UTC, matching the rest of the app. Times spoken to
the customer are converted to the company's own timezone at the edge.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlmodel import Session, select

import gcal
from models import Appointment, Company, CompanySettings, utcnow


class BookingError(Exception):
    """A booking that cannot be made, with a reason safe to speak aloud."""


def _tz(company: Company) -> ZoneInfo:
    try:
        return ZoneInfo(company.timezone or "Asia/Kolkata")
    except (ZoneInfoNotFoundError, ValueError):
        return ZoneInfo("UTC")


def to_utc(local_naive: datetime, company: Company) -> datetime:
    """A wall-clock time in the company's zone -> naive UTC for storage."""
    return (
        local_naive.replace(tzinfo=_tz(company))
        .astimezone(timezone.utc)
        .replace(tzinfo=None)
    )


def to_local(utc_naive: datetime, company: Company) -> datetime:
    """Stored naive UTC -> wall-clock time in the company's zone."""
    return utc_naive.replace(tzinfo=timezone.utc).astimezone(_tz(company)).replace(tzinfo=None)


def parse_when(text: str) -> datetime | None:
    """Parse the ISO local datetime the model proposes, e.g. 2026-08-05T11:00.

    Deliberately strict: a model that invents "next Tuesday" instead of a real
    timestamp should fail here and be treated as "no booking", rather than
    having us guess a date and confirm the wrong appointment to a customer.
    """
    raw = (text or "").strip().replace(" ", "T")[:16]
    for fmt in ("%Y-%m-%dT%H:%M", "%Y-%m-%dT%H"):
        try:
            return datetime.strptime(raw, fmt)
        except ValueError:
            continue
    return None


def _within_hours(local: datetime, s: CompanySettings) -> bool:
    try:
        open_t = datetime.strptime(s.booking_open or "10:00", "%H:%M").time()
        close_t = datetime.strptime(s.booking_close or "18:00", "%H:%M").time()
    except ValueError:
        return True
    return open_t <= local.time() <= close_t


def clashes(db: Session, company_id: int, start: datetime, end: datetime,
            exclude_id: int | None = None) -> Appointment | None:
    """The existing appointment overlapping [start, end), or None.

    Overlap test is `existing.start < new.end AND existing.end > new.start`,
    which is the only form that treats back-to-back bookings as compatible -
    an 11:00-11:30 and a 11:30-12:00 do not clash.
    """
    rows = db.exec(
        select(Appointment).where(
            Appointment.company_id == company_id,
            Appointment.status == "booked",
            Appointment.starts_at < end,
            Appointment.ends_at > start,
        )
    ).all()
    for row in rows:
        if exclude_id and row.id == exclude_id:
            continue
        return row
    return None


def next_free(db: Session, company: Company, s: CompanySettings,
              after_local: datetime, limit: int = 40) -> datetime | None:
    """The first free slot at or after `after_local`, in company-local time.

    Walks forward slot by slot rather than doing anything clever, because the
    search space is small (a few days of a working day) and being obviously
    correct matters more here than being fast.
    """
    slot = max(1, s.booking_slot_minutes or 30)
    try:
        open_t = datetime.strptime(s.booking_open or "10:00", "%H:%M").time()
        close_t = datetime.strptime(s.booking_close or "18:00", "%H:%M").time()
    except ValueError:
        open_t, close_t = datetime.strptime("10:00", "%H:%M").time(), datetime.strptime("18:00", "%H:%M").time()

    # Round up to the next slot boundary so we never suggest 11:07.
    minute = (after_local.minute // slot) * slot
    cur = after_local.replace(minute=minute, second=0, microsecond=0)
    if cur < after_local:
        cur += timedelta(minutes=slot)

    for _ in range(limit):
        if cur.time() > close_t:                       # past closing - next day
            cur = (cur + timedelta(days=1)).replace(hour=open_t.hour, minute=open_t.minute)
            continue
        if cur.time() < open_t:
            cur = cur.replace(hour=open_t.hour, minute=open_t.minute)

        start_utc = to_utc(cur, company)
        if not clashes(db, company.id, start_utc, start_utc + timedelta(minutes=slot)):
            return cur
        cur += timedelta(minutes=slot)
    return None


def speak_time(local: datetime) -> str:
    """A datetime as an agent would say it, not as a computer would print it."""
    hour = local.hour % 12 or 12
    ampm = "AM" if local.hour < 12 else "PM"
    minute = f":{local.minute:02d}" if local.minute else ""
    return f"{local.strftime('%A %d %B')} at {hour}{minute} {ampm}"


def book(
    db: Session,
    company: Company,
    s: CompanySettings,
    when_local: datetime,
    *,
    phone: str,
    customer_name: str = "",
    call_sid: str = "",
    campaign_id: int | None = None,
    contact_id: int | None = None,
    notes: str = "",
) -> Appointment:
    """Reserve a slot. Raises BookingError with a speakable reason if it can't."""
    slot = max(1, s.booking_slot_minutes or 30)
    start = to_utc(when_local, company)
    end = start + timedelta(minutes=slot)

    if start < utcnow():
        raise BookingError("that time has already passed")

    horizon = utcnow() + timedelta(days=max(1, s.booking_days_ahead or 30))
    if start > horizon:
        raise BookingError(f"we only book up to {s.booking_days_ahead} days ahead")

    if not _within_hours(when_local, s):
        raise BookingError(
            f"we only take appointments between {s.booking_open} and {s.booking_close}"
        )

    if clashes(db, company.id, start, end):
        raise BookingError("that slot is already taken")

    # Also ask Google, if linked. Without this the two diaries drift: someone
    # blocks 11am in Google Calendar, we know nothing about it, and the agent
    # books straight over the top.
    if gcal.is_busy(s, start, end):
        raise BookingError("that slot is already taken")

    appt = Appointment(
        company_id=company.id,
        campaign_id=campaign_id,
        contact_id=contact_id,
        call_sid=call_sid,
        customer_name=customer_name,
        phone=phone,
        starts_at=start,
        ends_at=end,
        notes=notes,
    )
    db.add(appt)
    db.commit()
    db.refresh(appt)

    # Mirror into Google. Deliberately after the local commit and non-fatal:
    # the appointment is real whether or not the copy lands.
    event_id = gcal.create_event(
        s,
        summary=f"{customer_name or phone} - appointment",
        description=(
            f"Booked by SancharAI during a call.\nPhone: {phone}\n{notes}"
        ).strip(),
        start_utc=start,
        end_utc=end,
        tz_name=company.timezone or "Asia/Kolkata",
    )
    if event_id:
        appt.gcal_event_id = event_id
        db.add(appt)
        db.commit()
        db.refresh(appt)
    return appt


def try_book(db: Session, company: Company, s: CompanySettings, when_text: str,
             **kwargs) -> tuple[Appointment | None, str]:
    """Attempt a booking. Returns (appointment, message_to_speak).

    On success the message is empty and the model's own confirmation is used.
    On failure the message REPLACES it, so the customer is never told a slot is
    confirmed when it isn't - and is offered a real alternative rather than a
    dead end.
    """
    when_local = parse_when(when_text)
    if not when_local:
        return None, ""   # not a real time - treat as no booking attempt

    try:
        return book(db, company, s, when_local, **kwargs), ""
    except BookingError as exc:
        alt = next_free(db, company, s, when_local)
        if alt:
            return None, f"Sorry, {exc}. The next free slot is {speak_time(alt)}. Does that work?"
        return None, f"Sorry, {exc}. Could we find another time?"

"""
Background campaign worker.

This replaces two broken designs at once:

  v1 placed every call inline inside POST /api/call, looping over the customer
  list and making a synchronous Twilio HTTP request per number from inside an
  async endpoint. Uploading 500 contacts froze the entire event loop - every
  other tenant's dashboard hung until the last call was dialed.

  v2 had a background worker, but compared the call window against UTC. A user
  setting 09:00-20:00 expecting IST actually got 14:30-01:30 IST, so the agent
  rang people at half past one in the morning. Windows are evaluated here in
  the company's own timezone.

Only ONE process may run this loop. Two workers would each pick up the same
pending contacts and dial every customer twice - the exact bug v2 shipped with.
A lock file with a heartbeat enforces that.
"""
from __future__ import annotations

import asyncio
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlmodel import Session, col, func, select

from models import (
    Call,
    Campaign,
    Company,
    CompanySettings,
    Contact,
    engine,
    to_local,
    utcnow,
)
import engine as call_engine
import plans
import telephony

TICK_SECONDS = int(os.getenv("SANCHARAI_SCHEDULER_TICK", "15"))
BATCH_PER_TICK = int(os.getenv("SANCHARAI_SCHEDULER_BATCH", "3"))
LOCK_FILE = Path(__file__).parent / ".scheduler.lock"
LOCK_STALE_SECONDS = 120

RETRY_DELAYS = {
    "No retry": None,
    "After 2 hours": timedelta(hours=2),
    "After 4 hours": timedelta(hours=4),
    "Next day": timedelta(days=1),
}


# ------------------------------------------------------------- instance lock
def _acquire_lock() -> bool:
    """True if this process owns the scheduler.

    The lock is a file holding "<pid> <unix_ts>". A lock older than
    LOCK_STALE_SECONDS is assumed to belong to a crashed process and is taken
    over, so a hard kill never permanently wedges the scheduler.
    """
    now = time.time()
    if LOCK_FILE.exists():
        try:
            pid_s, ts_s = LOCK_FILE.read_text().split()
            if now - float(ts_s) < LOCK_STALE_SECONDS and int(pid_s) != os.getpid():
                return False
        except (ValueError, OSError):
            pass  # unreadable lock: treat as stale
    try:
        LOCK_FILE.write_text(f"{os.getpid()} {now}")
        return True
    except OSError:
        return False


def _heartbeat() -> None:
    try:
        LOCK_FILE.write_text(f"{os.getpid()} {time.time()}")
    except OSError:
        pass


def release_lock() -> None:
    try:
        if LOCK_FILE.exists():
            pid_s, _ = LOCK_FILE.read_text().split()
            if int(pid_s) == os.getpid():
                LOCK_FILE.unlink()
    except (ValueError, OSError):
        pass


# ------------------------------------------------------------- window / caps
def in_call_window(campaign: Campaign, tz_name: str) -> bool:
    """Evaluate the campaign's window in the COMPANY's timezone.

    Also handles a window that wraps past midnight (e.g. 21:00-06:00), which
    a naive `start <= now <= end` gets wrong.
    """
    try:
        start = datetime.strptime(campaign.call_window_start, "%H:%M").time()
        end = datetime.strptime(campaign.call_window_end, "%H:%M").time()
    except (ValueError, TypeError):
        return True  # unparseable window: don't block the campaign

    now = to_local(utcnow(), tz_name).time()
    return start <= now <= end if start <= end else (now >= start or now <= end)


def _local_day_start_utc(tz_name: str) -> datetime:
    """Midnight today in the company's timezone, expressed as naive UTC.

    The daily cap has to reset at the customer's midnight, not UTC's. v1
    compared naive-UTC timestamps against a local date and drifted by the
    offset; in IST that moved the reset to 05:30.
    """
    try:
        tz = ZoneInfo(tz_name or "Asia/Kolkata")
    except (ZoneInfoNotFoundError, ValueError):
        tz = ZoneInfo("UTC")
    local_midnight = (
        datetime.now(timezone.utc)
        .astimezone(tz)
        .replace(hour=0, minute=0, second=0, microsecond=0)
    )
    return local_midnight.astimezone(timezone.utc).replace(tzinfo=None)


def calls_made_today(db: Session, campaign: Campaign, tz_name: str) -> int:
    since = _local_day_start_utc(tz_name)
    return db.exec(
        select(func.count())
        .select_from(Call)
        .where(Call.campaign_id == campaign.id, Call.created_at >= since)
    ).one()


# ------------------------------------------------------------------- dialing
def _schedule_retry(contact: Contact, campaign: Campaign, error: str, db: Session) -> None:
    """Apply the campaign's retry policy. v1 stored retry_policy on the model
    and never once read it, so a failed call was simply lost."""
    delay = RETRY_DELAYS.get(campaign.retry_policy)
    contact.retry_count += 1
    contact.last_error = (error or "")[:200]

    if delay is None or contact.retry_count >= max(1, campaign.max_retries):
        contact.status = "failed"
        contact.next_attempt_at = None
    else:
        contact.status = "pending"
        contact.next_attempt_at = utcnow() + delay
    db.add(contact)
    db.commit()


def _dial_one(
    db: Session,
    company: Company,
    settings: CompanySettings,
    campaign: Campaign,
    contact: Contact,
    webhook_base: str,
) -> bool:
    """Place a single call. Runs in a worker thread - the provider SDK is
    blocking, so calling it on the event loop would stall every request."""
    try:
        sid, provider = telephony.place_call(settings, webhook_base, contact.phone)
    except Exception as exc:
        _schedule_retry(contact, campaign, str(exc), db)
        print(f"[scheduler] dial failed {contact.phone}: {exc}", flush=True)
        return False

    call_engine.start_session(
        db,
        call_sid=sid,
        company_id=company.id,
        customer=contact.as_customer(),
        campaign_id=campaign.id,
        contact_id=contact.id,
        language=campaign.speech_language,
        provider=provider,
    )
    contact.status = "dialed"
    contact.call_sid = sid
    contact.dialed_at = utcnow()
    db.add(contact)
    db.commit()
    print(f"[scheduler] dialed {contact.phone} -> {sid[:12]}", flush=True)
    return True


def _public_base() -> str:
    """Where Twilio should call back.

    The worker has no incoming request to read a Host header from, so this must
    be configured. v2 silently marked contacts as errored when it was missing;
    here the campaign is paused with a reason the UI can display.
    """
    return os.getenv("PUBLIC_BASE_URL", "").strip().rstrip("/")


def _pause(campaign: Campaign, reason: str, db: Session) -> None:
    campaign.status = "paused"
    campaign.pause_reason = reason
    db.add(campaign)
    db.commit()
    print(f"[scheduler] paused campaign {campaign.id}: {reason}", flush=True)


def tick() -> None:
    """One pass over every running campaign. Synchronous by design; the async
    loop hands it to a thread."""
    base = _public_base()

    with Session(engine) as db:
        running = db.exec(select(Campaign).where(Campaign.status == "running")).all()
        for campaign in running:
            company = db.get(Company, campaign.company_id)
            if not company:
                continue

            if not base:
                _pause(campaign, "PUBLIC_BASE_URL is not set", db)
                continue

            # Plan minute cap
            try:
                plans.check_minutes(company)
            except plans.PlanLimitError:
                _pause(campaign, "Monthly minute limit reached", db)
                continue

            # Call window, in the company's own timezone
            if not in_call_window(campaign, company.timezone):
                continue

            # Daily cap, reset at the company's local midnight
            made = calls_made_today(db, campaign, company.timezone)
            remaining = max(0, campaign.max_calls_per_day - made)
            if remaining <= 0:
                continue

            settings = db.exec(
                select(CompanySettings).where(CompanySettings.company_id == company.id)
            ).first()
            if not settings or not settings.twilio_account_sid:
                _pause(campaign, "Twilio is not connected", db)
                continue

            now = utcnow()
            contacts = db.exec(
                select(Contact)
                .where(
                    Contact.campaign_id == campaign.id,
                    Contact.status == "pending",
                    (col(Contact.next_attempt_at).is_(None))
                    | (col(Contact.next_attempt_at) <= now),
                )
                .limit(min(BATCH_PER_TICK, remaining))
            ).all()

            if not contacts:
                # Anything still pending is just waiting on a retry timer.
                waiting = db.exec(
                    select(func.count())
                    .select_from(Contact)
                    .where(Contact.campaign_id == campaign.id, Contact.status == "pending")
                ).one()
                if not waiting:
                    campaign.status = "completed"
                    campaign.pause_reason = ""
                    db.add(campaign)
                    db.commit()
                    print(f"[scheduler] campaign {campaign.id} completed", flush=True)
                continue

            webhook_base = f"{base}/hook/{company.webhook_token}"
            for contact in contacts:
                _dial_one(db, company, settings, campaign, contact, webhook_base)

            campaign.last_run_at = utcnow()
            db.add(campaign)
            db.commit()


async def loop() -> None:
    """Entry point started from the FastAPI lifespan handler."""
    if os.getenv("SANCHARAI_SCHEDULER", "1").strip() == "0":
        print("[scheduler] disabled (SANCHARAI_SCHEDULER=0)", flush=True)
        return

    await asyncio.sleep(3)
    if not _acquire_lock():
        print(
            "[scheduler] another instance holds the lock - not starting. "
            "Only one process may dial, or every contact gets called twice.",
            flush=True,
        )
        return

    print(f"[scheduler] started (pid {os.getpid()}, every {TICK_SECONDS}s)", flush=True)
    try:
        while True:
            try:
                _heartbeat()
                # to_thread keeps the blocking Twilio + SQLite work off the loop.
                await asyncio.to_thread(tick)
            except Exception as exc:
                print(f"[scheduler] tick error: {exc}", flush=True)
            await asyncio.sleep(TICK_SECONDS)
    finally:
        release_lock()

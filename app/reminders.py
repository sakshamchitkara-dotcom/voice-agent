"""Reminders: a scheduled callback (Vapi outbound call with schedulePlan) or a timed SMS.

Callbacks use Vapi's POST /call with `schedulePlan.earliestAt` (docs.vapi.ai/api-reference/calls/create),
so Vapi places the call even if this server is down at that time. SMS reminders are sent by
this server's dispatcher loop through notify.send. Both are dry-run unless DRY_RUN=false and
the channel is configured, and both only ever go to the caller's own number.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import httpx

from . import db, notify
from .config import Settings, get_settings
from .logs import log_event

VAPI_API = "https://api.vapi.ai"
MAX_AHEAD = timedelta(days=30)
CALL_WINDOW = timedelta(minutes=10)  # schedulePlan.latestAt = earliestAt + this


def parse_due(value: str, tz: str) -> datetime:
    """ISO local time in TIMEZONE (or with an explicit offset) -> aware UTC datetime."""
    try:
        when = datetime.fromisoformat(value.strip())
    except ValueError:
        raise ValueError("when must be an ISO date-time like 2026-09-26T15:00") from None
    if when.tzinfo is None:
        when = when.replace(tzinfo=ZoneInfo(tz))
    when = when.astimezone(timezone.utc)
    now = datetime.now(timezone.utc)
    if when < now + timedelta(minutes=1):
        raise ValueError("That time has already passed. Pick a time in the future.")
    if when > now + MAX_AHEAD:
        raise ValueError("I can only set reminders up to 30 days ahead.")
    return when


def call_configured(s: Settings) -> bool:
    return bool(s.vapi_api_key and s.vapi_phone_number_id)


def callback_payload(s: Settings, to: str, when: datetime, message: str) -> dict:
    from .assistant import build_assistant  # assistant -> tools -> reminders

    assistant = build_assistant(s, trusted=True, name="Reminder callback")
    assistant["firstMessage"] = f"Hi, it's your assistant with the reminder you asked for: {message}"
    return {
        "name": f"Reminder: {message}"[:40],
        "phoneNumberId": s.vapi_phone_number_id,
        "customer": {"number": to},
        "schedulePlan": {"earliestAt": when.isoformat().replace("+00:00", "Z"),
                         "latestAt": (when + CALL_WINDOW).isoformat().replace("+00:00", "Z")},
        "assistant": assistant,
    }


async def _vapi_create_call(s: Settings, payload: dict) -> str:
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.post(f"{VAPI_API}/call", json=payload,
                              headers={"Authorization": f"Bearer {s.vapi_api_key}"})
        r.raise_for_status()
        return r.json().get("id", "")


async def schedule(call_id: str | None, caller: str, kind: str, when: datetime, message: str) -> tuple[int, str]:
    """Record the reminder; for callbacks also ask Vapi to schedule the call. Returns (id, status)."""
    s = get_settings()
    status, vapi_call_id = "pending", None
    if kind == "call":
        if s.dry_run or not call_configured(s):
            status = "dry-run"
            log_event("reminder.call_dry_run", to=caller, due=when.isoformat())
        else:
            try:
                vapi_call_id = await _vapi_create_call(s, callback_payload(s, caller, when, message))
            except httpx.HTTPError as e:
                log_event("reminder.vapi_failed", logging.ERROR, error=str(e))
                raise ValueError("I couldn't schedule the callback with the phone system.") from None
            status = "scheduled"
    with db.connect() as conn:
        rid = conn.execute(
            "INSERT INTO reminders (call_id, caller, kind, due_at, message, status, vapi_call_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (call_id, caller, kind, when.isoformat(timespec="seconds"), message, status, vapi_call_id),
        ).lastrowid
    log_event("reminder.created", reminder_id=rid, kind=kind, status=status)
    return rid, status


async def dispatch_due(now: datetime | None = None) -> int:
    """Send SMS reminders that are due. Each one is claimed atomically so it goes out once."""
    now = now or datetime.now(timezone.utc)
    with db.connect() as conn:
        due = conn.execute("SELECT * FROM reminders WHERE kind = 'sms' AND status = 'pending' "
                           "AND due_at <= ?", (now.isoformat(timespec="seconds"),)).fetchall()
    sent = 0
    for r in due:
        with db.connect() as conn:
            if not conn.execute("UPDATE reminders SET status = 'sending' WHERE id = ? AND status = 'pending'",
                                (r["id"],)).rowcount:
                continue
        try:
            result = await notify.send("sms", r["caller"], "Reminder", f"Reminder: {r['message']}")
            status = "dry-run" if result.startswith("dry-run") else "sent"
        except Exception as e:
            status = "failed"
            log_event("reminder.failed", logging.ERROR, reminder_id=r["id"], error=str(e))
        with db.connect() as conn:
            conn.execute("UPDATE reminders SET status = ? WHERE id = ?", (status, r["id"]))
        sent += 1
    return sent


async def run_dispatcher(interval_s: float = 30.0) -> None:
    """Background loop started with the app. ponytail: single instance; polling, not a queue."""
    while True:
        try:
            await dispatch_due()
        except Exception as e:  # keep the loop alive
            log_event("reminder.dispatch_error", logging.ERROR, error=repr(e))
        await asyncio.sleep(interval_s)

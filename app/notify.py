"""Follow-up delivery by email (SMTP) or SMS (Twilio). Dry-run by default.

Every attempt is written to the outbox table so dry runs are inspectable.
"""
from __future__ import annotations

import asyncio
import logging
import smtplib
from email.message import EmailMessage

import httpx

from . import db
from .config import Settings, get_settings
from .logs import log_event


def _record(channel: str, to: str, subject: str, body: str, status: str) -> None:
    with db.connect() as conn:
        conn.execute(
            "INSERT INTO outbox (channel, recipient, subject, body, status) VALUES (?, ?, ?, ?, ?)",
            (channel, to, subject, body, status),
        )


def _smtp_send(s: Settings, to: str, subject: str, body: str) -> None:
    msg = EmailMessage()
    msg["From"], msg["To"], msg["Subject"] = s.smtp_from or s.smtp_user, to, subject
    msg.set_content(body)
    with smtplib.SMTP(s.smtp_host, s.smtp_port, timeout=20) as smtp:
        smtp.starttls()
        if s.smtp_user:
            smtp.login(s.smtp_user, s.smtp_password)
        smtp.send_message(msg)


async def _twilio_send(s: Settings, to: str, body: str) -> None:
    url = f"https://api.twilio.com/2010-04-01/Accounts/{s.twilio_account_sid}/Messages.json"
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.post(url, data={"To": to, "From": s.twilio_from, "Body": body[:1500]},
                              auth=(s.twilio_account_sid, s.twilio_auth_token))
        r.raise_for_status()


def will_send(channel: str) -> bool:
    """True only if a message on this channel would really go out (not a dry run)."""
    s = get_settings()
    configured = bool(s.smtp_host) if channel == "email" else bool(s.twilio_account_sid)
    return configured and not s.dry_run


async def send(channel: str, to: str, subject: str, body: str) -> str:
    """Send (or dry-run) a message. Returns a short spoken-friendly status."""
    s = get_settings()
    if not will_send(channel):
        _record(channel, to, subject, body, "dry-run")
        log_event("notify.dry_run", channel=channel, to=to, chars=len(body))
        return f"dry-run: {channel} to {to} recorded but not sent"
    try:
        if channel == "email":
            await asyncio.to_thread(_smtp_send, s, to, subject, body)
        else:
            await _twilio_send(s, to, body)
    except Exception as e:
        _record(channel, to, subject, body, "failed")
        log_event("notify.failed", logging.ERROR, channel=channel, to=to, error=str(e))
        raise
    _record(channel, to, subject, body, "sent")
    log_event("notify.sent", channel=channel, to=to)
    return f"{channel} sent to {to}"

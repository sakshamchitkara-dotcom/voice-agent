"""Post-call email to the owner: who called, how long, the summary, what the assistant did.

Off unless POST_CALL_EMAIL=true and OWNER_EMAIL is set. Goes through notify.send, so it is
a dry run (recorded in the outbox) unless DRY_RUN=false and SMTP is configured.
"""
from __future__ import annotations

from collections import Counter
from datetime import datetime

from . import db, notify
from .config import get_settings

TEMPLATE = """\
{who} called{when}.
Length: {length} · Ended: {ended}

Summary
{summary}

What I did
{actions}

Follow-ups
{followups}

Details: {link}
"""


def _length(started: str | None, ended: str | None) -> str:
    try:
        secs = int((datetime.fromisoformat(ended.replace("Z", "+00:00"))
                    - datetime.fromisoformat(started.replace("Z", "+00:00"))).total_seconds())
    except (AttributeError, ValueError):
        return "unknown"
    return f"{secs // 60} min {secs % 60:02d} s"


def render(call_id: str) -> tuple[str, str] | None:
    """(subject, body) for a stored call, or None if we don't have it."""
    call = db.get_call(call_id)
    if call is None:
        return None
    with db.connect() as conn:
        tools = Counter(r[0] for r in conn.execute(
            "SELECT tool FROM tool_calls WHERE call_id = ? AND outcome = 'ok'", (call_id,)))
        jobs = conn.execute("SELECT task, channel FROM jobs WHERE call_id = ?", (call_id,)).fetchall()
        rems = conn.execute("SELECT kind, due_at, message FROM reminders WHERE call_id = ?",
                            (call_id,)).fetchall()
    who = call["caller"] or ("A web caller" if call["type"] == "webCall" else "Someone")
    length = _length(call["started_at"], call["ended_at"])
    actions = "\n".join(f"- {t}" + (f" ×{n}" if n > 1 else "") for t, n in sorted(tools.items()))
    followups = [f"- Deep task by {j['channel']}: {j['task']}" for j in jobs]
    followups += [f"- Reminder ({r['kind']}) at {r['due_at']}: {r['message']}" for r in rems]
    body = TEMPLATE.format(
        who=who, when=f" at {call['started_at']}" if call["started_at"] else "",
        length=length, ended=call["ended_reason"] or "unknown",
        summary=call["summary"] or "(no summary)", actions=actions or "- nothing, just talked",
        followups="\n".join(followups) or "- none", link=f"{get_settings().public_url}/admin/calls/{call_id}")
    return f"Call from {who} ({length})", body


async def email_owner(call_id: str) -> str | None:
    s = get_settings()
    if not (s.post_call_email and s.owner_email):
        return None
    rendered = render(call_id)
    if rendered is None:
        return None
    subject, body = rendered
    return await notify.send("email", s.owner_email, subject, body)

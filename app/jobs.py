"""Deep tasks: longer Claude-powered jobs queued during a call, delivered after it ends."""
from __future__ import annotations

import asyncio
import logging

from . import db, llm, notify, web_tools
from .config import get_settings
from .logs import log_event

DEEP_SYSTEM = (
    "You are a research assistant finishing a task a user asked for during a phone call. "
    "Research with web search where it helps, verify claims, and cite source URLs inline. "
    "Write the final answer as plain text (no markdown tables), under 400 words, starting "
    "with a one-sentence answer, then the supporting detail. It will be sent by email or SMS."
)

_running: set[asyncio.Task] = set()


def _spawn(coro) -> None:
    task = asyncio.create_task(coro)
    _running.add(task)
    task.add_done_callback(_running.discard)


def enqueue(call_id: str | None, caller: str | None, task: str, channel: str, recipient: str) -> int:
    with db.connect() as conn:
        job_id = conn.execute(
            "INSERT INTO jobs (call_id, caller, task, channel, recipient) VALUES (?, ?, ?, ?, ?)",
            (call_id, caller, task, channel, recipient),
        ).lastrowid
    log_event("job.queued", job_id=job_id, call_id=call_id, channel=channel)
    _spawn(run_job(job_id))
    return job_id


def _set(job_id: int, **fields) -> None:
    cols = ", ".join(f"{k} = ?" for k in fields)
    with db.connect() as conn:
        conn.execute(f"UPDATE jobs SET {cols}, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                     (*fields.values(), job_id))


def get_job(job_id: int):
    with db.connect() as conn:
        return conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()


def _claim(job_id: int) -> bool:
    """queued -> running, atomically: with several workers only one runs a given job."""
    with db.connect() as conn:
        return bool(conn.execute("UPDATE jobs SET status = 'running', updated_at = CURRENT_TIMESTAMP "
                                 "WHERE id = ? AND status = 'queued'", (job_id,)).rowcount)


async def run_job(job_id: int) -> None:
    if not _claim(job_id):
        return
    job = get_job(job_id)
    try:
        text = await llm.complete(job["task"], DEEP_SYSTEM, effort="high",
                                  max_tokens=16000, web_search=True)
        if text is None:  # Claude unavailable: degrade to raw search results
            results = await web_tools.search(job["task"])
            text = ("Claude was unavailable, so here are the top web results: "
                    + web_tools.format_results(results))
        _set(job_id, status="done", result=text)
        log_event("job.done", job_id=job_id, chars=len(text))
    except Exception as e:
        _set(job_id, status="failed", result=f"Sorry, the task failed: {e}")
        log_event("job.failed", logging.ERROR, job_id=job_id, error=str(e))
    await deliver_if_ready(job_id)


async def deliver_if_ready(job_id: int) -> bool:
    """Deliver a finished job once its call has ended (or if we never saw the call)."""
    job = get_job(job_id)
    if job is None or job["status"] not in ("done", "failed") or job["delivered"]:
        return False
    call = db.get_call(job["call_id"]) if job["call_id"] else None
    if call is not None and call["status"] != "ended":
        return False
    with db.connect() as conn:  # claim atomically so we never double-send
        claimed = conn.execute(
            "UPDATE jobs SET delivered = 1 WHERE id = ? AND delivered = 0", (job_id,)
        ).rowcount
    if not claimed:
        return False
    subject = f"Your voice-agent task: {job['task'][:60]}"
    try:
        await notify.send(job["channel"], job["recipient"], subject, job["result"])
    except Exception as e:
        log_event("job.delivery_failed", logging.ERROR, job_id=job_id, error=str(e))
        return False
    log_event("job.delivered", job_id=job_id, channel=job["channel"])
    return True


async def deliver_for_call(call_id: str) -> None:
    with db.connect() as conn:
        ids = [r["id"] for r in conn.execute(
            "SELECT id FROM jobs WHERE call_id = ? AND delivered = 0", (call_id,))]
    for job_id in ids:
        await deliver_if_ready(job_id)


def resume_pending() -> None:
    """On startup: restart interrupted jobs and retry undelivered finished ones.

    Single instance: any 'running' job was interrupted by the restart. With SHARED_STATE=sqlite
    another worker may still be running it, so only jobs untouched for 15 minutes are retaken.
    """
    age = "-15 minutes" if get_settings().shared_state == "sqlite" else "+1 minute"
    with db.connect() as conn:
        conn.execute("UPDATE jobs SET status = 'queued' WHERE status = 'running' AND delivered = 0 "
                     "AND updated_at <= datetime('now', ?)", (age,))
        rows = conn.execute("SELECT id, status FROM jobs WHERE delivered = 0").fetchall()
    for r in rows:
        if r["status"] == "queued":
            _spawn(run_job(r["id"]))
        elif r["status"] in ("done", "failed"):
            _spawn(deliver_if_ready(r["id"]))

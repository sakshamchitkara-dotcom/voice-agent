"""FastAPI service: the Vapi Server URL webhook plus health and web-call test page."""
from __future__ import annotations

import asyncio
import json
import logging
import os
from contextlib import asynccontextmanager

from fastapi import BackgroundTasks, FastAPI, Request
from fastapi.responses import JSONResponse

from . import db, jobs, llm, tools, vapi
from .assistant import build_assistant
from .config import get_settings
from .logs import log_event, setup_logging
from .security import is_trusted, verify_webhook


@asynccontextmanager
async def lifespan(app: FastAPI):
    setup_logging(os.getenv("LOG_LEVEL", "INFO"))
    db.init_db()
    jobs.resume_pending()
    log_event("server.started", public_url=get_settings().public_url)
    yield


app = FastAPI(title="voice-agent", lifespan=lifespan)

CALL_SUMMARY_SYSTEM = ("Summarise this phone call transcript in 2-3 sentences: what the caller "
                       "wanted, what was done, and any follow-ups promised.")


async def summarize_call(call_id: str, transcript: str) -> None:
    """Fallback when Vapi's end-of-call-report carries no analysis summary."""
    summary = await llm.complete(transcript[:100_000], CALL_SUMMARY_SYSTEM, effort="low", max_tokens=400)
    if summary:
        db.upsert_call(call_id, summary=summary)


@app.get("/healthz")
async def healthz() -> dict:
    return {"ok": True}


@app.post("/vapi/webhook")
async def vapi_webhook(request: Request, background: BackgroundTasks):
    s = get_settings()
    body = await request.body()
    if not verify_webhook(request.headers, body, s):
        log_event("webhook.unauthorized", logging.WARNING, ip=request.client and request.client.host)
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    try:
        message = json.loads(body).get("message") or {}
    except (json.JSONDecodeError, AttributeError):
        return JSONResponse({"error": "invalid JSON"}, status_code=400)

    kind = message.get("type")
    call = vapi.call_of(message)
    call_id, caller, ctype = call.get("id"), vapi.caller_number(message), vapi.call_type(message)
    log_event("webhook.received", type=kind, call_id=call_id, caller=caller)

    if kind == "assistant-request":
        trusted = is_trusted(caller, ctype, s)
        if call_id:
            db.upsert_call(call_id, caller=caller, type=ctype, status=call.get("status"))
        return {"assistant": build_assistant(s, trusted)}

    if kind == "tool-calls":
        ctx = tools.Ctx(call_id=call_id, caller=caller, trusted=is_trusted(caller, ctype, s))
        results = await asyncio.gather(*(tools.run(tc, ctx) for tc in vapi.tool_calls(message)))
        return {"results": list(results)}

    if kind == "status-update" and call_id:
        status = message.get("status")
        db.upsert_call(call_id, caller=caller, type=ctype, status=status,
                       ended_reason=message.get("endedReason"))
        if status == "ended":
            background.add_task(jobs.deliver_for_call, call_id)
        return {"ok": True}

    if kind == "end-of-call-report" and call_id:
        artifact = message.get("artifact") or {}
        transcript = artifact.get("transcript") or message.get("transcript")
        summary = (message.get("analysis") or {}).get("summary") or message.get("summary")
        db.upsert_call(
            call_id, caller=caller, type=ctype, status="ended",
            ended_reason=message.get("endedReason"), transcript=transcript, summary=summary,
            started_at=message.get("startedAt"), ended_at=message.get("endedAt"),
        )
        if transcript and not summary:
            background.add_task(summarize_call, call_id, transcript)
        background.add_task(jobs.deliver_for_call, call_id)
        return {"ok": True}

    return {"ok": True}

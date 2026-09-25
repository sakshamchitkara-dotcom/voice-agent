"""FastAPI service: the Vapi Server URL webhook plus health and web-call test page."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import BackgroundTasks, FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse

from . import db, jobs, llm, memory, metrics, reminders, tools, vapi
from .assistant import build_assistant
from .config import get_settings
from .logs import log_event, request_id, setup_logging
from .security import is_trusted, verify_webhook


@asynccontextmanager
async def lifespan(app: FastAPI):
    setup_logging(os.getenv("LOG_LEVEL", "INFO"))
    db.init_db()
    jobs.resume_pending()
    dispatcher = asyncio.create_task(reminders.run_dispatcher())
    log_event("server.started", public_url=get_settings().public_url)
    yield
    dispatcher.cancel()


app = FastAPI(title="voice-agent", lifespan=lifespan)

_SAFE_ID = re.compile(r"^[A-Za-z0-9._-]{1,64}$")


@app.middleware("http")
async def request_context(request: Request, call_next):
    """Tag each request with an ID (reuse a sane incoming X-Request-ID) for logs and replies."""
    incoming = request.headers.get("x-request-id", "")
    rid = incoming if _SAFE_ID.match(incoming) else uuid.uuid4().hex[:16]
    token = request_id.set(rid)
    start = time.perf_counter()
    status = 500
    try:
        response = await call_next(request)
        status = response.status_code
    finally:
        request_id.reset(token)
        # Route template, not the raw path, so metric labels stay bounded.
        route = getattr(request.scope.get("route"), "path", "unmatched")
        metrics.http_requests.inc(method=request.method, route=route, status=str(status))
        metrics.http_latency.observe(time.perf_counter() - start, method=request.method, route=route)
    response.headers["X-Request-ID"] = rid
    return response


@app.get("/metrics", include_in_schema=False)
async def prometheus_metrics() -> PlainTextResponse:
    return PlainTextResponse(metrics.render(), media_type="text/plain; version=0.0.4")

CALL_SUMMARY_SYSTEM = ("Summarise this phone call transcript in 2-3 sentences: what the caller "
                       "wanted, what was done, and any follow-ups promised.")


async def summarize_call(call_id: str, transcript: str) -> None:
    """Fallback when Vapi's end-of-call-report carries no analysis summary."""
    summary = await llm.complete(transcript[:100_000], CALL_SUMMARY_SYSTEM, effort="low", max_tokens=400)
    if summary:
        db.upsert_call(call_id, summary=summary)


WEB_DIR = Path(__file__).resolve().parent.parent / "web"


@app.get("/", include_in_schema=False)
async def web_call_page() -> FileResponse:
    return FileResponse(WEB_DIR / "index.html")


@app.get("/web/config")
async def web_config() -> dict:
    # The Vapi public key is designed to be exposed to browsers; never the private key.
    return {"publicKey": os.getenv("VAPI_PUBLIC_KEY", ""), "assistantId": os.getenv("VAPI_ASSISTANT_ID", "")}


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
    metrics.webhook_messages.inc(type=str(kind))
    call = vapi.call_of(message)
    call_id, caller, ctype = call.get("id"), vapi.caller_number(message), vapi.call_type(message)
    log_event("webhook.received", type=kind, call_id=call_id, caller=caller)

    if kind == "assistant-request":
        trusted = is_trusted(caller, ctype, s)
        if call_id:
            db.upsert_call(call_id, caller=caller, type=ctype, status=call.get("status"))
        memories = memory.facts_for(caller) if trusted else []
        return {"assistant": build_assistant(s, trusted, memories=memories)}

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
        if caller and is_trusted(caller, ctype, s):
            background.add_task(memory.remember_call, caller, call_id, message)
        background.add_task(jobs.deliver_for_call, call_id)
        return {"ok": True}

    return {"ok": True}

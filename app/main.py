"""FastAPI service: the Vapi Server URL webhook plus health and web-call test page."""
from __future__ import annotations

import asyncio
import json
import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from . import db, jobs, tools, vapi
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


@app.get("/healthz")
async def healthz() -> dict:
    return {"ok": True}


@app.post("/vapi/webhook")
async def vapi_webhook(request: Request):
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

    return {"ok": True}

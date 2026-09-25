"""Read-only admin dashboard: calls, transcripts, tool calls, outbox, deep-task jobs, reminders.

HTTP Basic auth with ADMIN_USER / ADMIN_PASSWORD. With no password set the dashboard is off
(404), so it is never accidentally public.
"""
from __future__ import annotations

import secrets
from html import escape
from typing import Iterable
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import HTMLResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials

from . import db, memory
from .config import get_settings

basic = HTTPBasic(auto_error=False)


def require_admin(creds: HTTPBasicCredentials | None = Depends(basic)) -> str:
    s = get_settings()
    if not s.admin_password:
        raise HTTPException(404)
    ok = creds is not None and (
        secrets.compare_digest(creds.username.encode(), s.admin_user.encode())
        & secrets.compare_digest(creds.password.encode(), s.admin_password.encode())
    )
    if not ok:
        raise HTTPException(401, headers={"WWW-Authenticate": 'Basic realm="voice-agent admin"'})
    return creds.username


router = APIRouter(prefix="/admin", dependencies=[Depends(require_admin)], include_in_schema=False)

CSS = """
:root{color-scheme:light dark;--fg:#1d1d1f;--bg:#fff;--mute:#6e6e73;--line:#e3e3e8;--acc:#0a66c2}
@media (prefers-color-scheme:dark){:root{--fg:#eee;--bg:#161618;--mute:#9a9aa0;--line:#2e2e33;--acc:#6aa9ff}}
body{font:14px/1.45 system-ui,sans-serif;color:var(--fg);background:var(--bg);margin:0 auto;
max-width:1100px;padding:16px}
nav a{margin-right:16px}a{color:var(--acc);text-decoration:none}h1{font-size:20px}
table{border-collapse:collapse;width:100%;margin:8px 0 24px}th,td{text-align:left;padding:6px 8px;
border-bottom:1px solid var(--line);vertical-align:top}th{color:var(--mute);font-weight:600}
td{max-width:420px;overflow-wrap:anywhere}pre{white-space:pre-wrap;background:rgba(127,127,127,.08);
padding:12px;border-radius:6px}.mute{color:var(--mute)}.error{color:#d33}.confirm{color:#b7791f}
"""


def page(title: str, body: str) -> HTMLResponse:
    html = (f"<!doctype html><html><head><meta charset=utf-8><meta name=viewport "
            f"content='width=device-width,initial-scale=1'><title>{escape(title)} · voice-agent</title>"
            f"<style>{CSS}</style></head><body><nav><a href='/admin'>Calls</a><a href='/admin/jobs'>Deep tasks</a>"
            f"<a href='/admin/outbox'>Outbox</a><a href='/admin/reminders'>Reminders</a>"
            f"<a href='/metrics'>Metrics</a></nav><h1>{escape(title)}</h1>{body}</body></html>")
    return HTMLResponse(html, headers={
        "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'",
        "X-Frame-Options": "DENY", "Cache-Control": "no-store", "Referrer-Policy": "no-referrer"})


def table(rows: Iterable, cols: list[str], link: dict[str, str] | None = None,
          limit: dict[str, int] | None = None) -> str:
    """rows: sqlite Rows or dicts. link: {col: url template using {value}}. limit: {col: chars}."""
    rows = list(rows)
    if not rows:
        return "<p class=mute>Nothing yet.</p>"
    link, limit = link or {}, limit or {}
    out = ["<table><tr>" + "".join(f"<th>{escape(c)}</th>" for c in cols) + "</tr>"]
    for r in rows:
        cells = []
        for c in cols:
            v = "" if r[c] is None else str(r[c])
            if c in limit and len(v) > limit[c]:
                v = v[: limit[c]] + "…"
            cell = escape(v)
            if c in link and v:
                cell = f"<a href='{escape(link[c].format(value=quote(v, safe='')))}'>{cell}</a>"
            cls = f" class={escape(v)}" if c == "outcome" and v in ("error", "confirm") else ""
            cells.append(f"<td{cls}>{cell}</td>")
        out.append("<tr>" + "".join(cells) + "</tr>")
    return "".join(out) + "</table>"


def q(sql: str, *args) -> list:
    with db.connect() as conn:
        return conn.execute(sql, args).fetchall()


@router.get("", response_class=HTMLResponse)
async def calls() -> HTMLResponse:
    rows = q("SELECT c.*, (SELECT COUNT(*) FROM tool_calls t WHERE t.call_id = c.id) AS tools "
             "FROM calls c ORDER BY c.updated_at DESC LIMIT 200")
    return page("Calls", table(rows, ["id", "caller", "type", "status", "ended_reason", "tools",
                                      "started_at", "summary"],
                               link={"id": "/admin/calls/{value}"}, limit={"summary": 140}))


@router.get("/calls/{call_id}", response_class=HTMLResponse)
async def call_detail(call_id: str) -> HTMLResponse:
    call = db.get_call(call_id)
    if call is None:
        raise HTTPException(404)
    meta = table([call], ["caller", "type", "status", "ended_reason", "started_at", "ended_at"])
    parts = [meta,
             f"<h2>Summary</h2><p>{escape(call['summary'] or '—')}</p>",
             f"<h2>Transcript</h2><pre>{escape(call['transcript'] or '—')}</pre>",
             "<h2>Tool calls</h2>" + table(
                 q("SELECT created_at, tool, args, outcome, output, ms, request_id FROM tool_calls "
                   "WHERE call_id = ? ORDER BY id", call_id),
                 ["created_at", "tool", "args", "outcome", "output", "ms", "request_id"],
                 limit={"output": 400}),
             "<h2>Deep tasks</h2>" + table(
                 q("SELECT id, status, delivered, task, result FROM jobs WHERE call_id = ?", call_id),
                 ["id", "status", "delivered", "task", "result"], limit={"result": 300}),
             "<h2>Reminders</h2>" + table(
                 q("SELECT id, kind, due_at, status, message FROM reminders WHERE call_id = ?", call_id),
                 ["id", "kind", "due_at", "status", "message"])]
    if call["caller"]:
        facts = [{"fact": f} for f in memory.facts_for(call["caller"], memory.MAX_FACTS_PER_CALLER)]
        parts.append(f"<h2>Memory for {escape(call['caller'])}</h2>" + table(facts, ["fact"]))
    return page(f"Call {call_id}", "".join(parts))


@router.get("/jobs", response_class=HTMLResponse)
async def jobs_page() -> HTMLResponse:
    rows = q("SELECT * FROM jobs ORDER BY id DESC LIMIT 200")
    return page("Deep tasks", table(rows, ["id", "call_id", "status", "delivered", "channel", "recipient",
                                           "task", "result", "updated_at"],
                                    link={"call_id": "/admin/calls/{value}"},
                                    limit={"task": 160, "result": 300}))


@router.get("/outbox", response_class=HTMLResponse)
async def outbox_page() -> HTMLResponse:
    rows = q("SELECT * FROM outbox ORDER BY id DESC LIMIT 200")
    return page("Outbox", table(rows, ["id", "created_at", "channel", "recipient", "status", "subject", "body"],
                                limit={"body": 300}))


@router.get("/reminders", response_class=HTMLResponse)
async def reminders_page() -> HTMLResponse:
    rows = q("SELECT * FROM reminders ORDER BY id DESC LIMIT 200")
    return page("Reminders", table(rows, ["id", "call_id", "caller", "kind", "due_at", "status",
                                          "vapi_call_id", "message"],
                                   link={"call_id": "/admin/calls/{value}"}))

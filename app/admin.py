"""Read-only admin dashboard: calls, transcripts, tool calls, outbox, deep-task jobs, reminders.

HTTP Basic auth with ADMIN_USER / ADMIN_PASSWORD. With no password set the dashboard is off
(404), so it is never accidentally public.
"""
from __future__ import annotations

import re
import secrets
from datetime import datetime, timezone
from html import escape
from typing import Iterable
from urllib.parse import quote

from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import HTMLResponse, Response
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
.bar{display:inline-block;height:10px;background:var(--acc);border-radius:2px;vertical-align:middle}
"""


def page(title: str, body: str) -> HTMLResponse:
    html = (f"<!doctype html><html><head><meta charset=utf-8><meta name=viewport "
            f"content='width=device-width,initial-scale=1'><title>{escape(title)} · voice-agent</title>"
            f"<style>{CSS}</style></head><body><nav><a href='/admin'>Calls</a>"
            f"<a href='/admin/analytics'>Analytics</a><a href='/admin/jobs'>Deep tasks</a>"
            f"<a href='/admin/outbox'>Outbox</a><a href='/admin/reminders'>Reminders</a>"
            f"<a href='/admin/calendar.ics'>Calendar (.ics)</a>"
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


def pct(values: list[int], p: float) -> int | None:
    """Nearest-rank percentile of a list of ms values."""
    if not values:
        return None
    ordered = sorted(values)
    return ordered[max(0, -(-len(ordered) * p // 100) - 1)]


def analytics_data(days: int = 14) -> dict:
    since = f"-{days} days"
    per_day = q("SELECT date(COALESCE(started_at, updated_at)) AS day, COUNT(*) AS calls FROM calls "
                "WHERE COALESCE(started_at, updated_at) >= date('now', ?) GROUP BY day ORDER BY day", since)
    reasons = q("SELECT COALESCE(ended_reason, '(none yet)') AS ended_reason, COUNT(*) AS calls "
                "FROM calls GROUP BY 1 ORDER BY 2 DESC")
    tools: dict[str, dict] = {}
    for r in q("SELECT tool, outcome, ms FROM tool_calls WHERE created_at >= date('now', ?)", since):
        t = tools.setdefault(r["tool"], {"tool": r["tool"], "calls": 0, "ok": 0, "error": 0,
                                         "confirm": 0, "deferred": 0, "_ms": []})
        t["calls"] += 1
        t[r["outcome"]] = t.get(r["outcome"], 0) + 1
        if r["ms"] is not None:
            t["_ms"].append(r["ms"])
    for t in tools.values():
        t["error_rate"] = f"{100 * t['error'] / t['calls']:.0f}%"
        t["p50_ms"], t["p95_ms"] = pct(t["_ms"], 50), pct(t["_ms"], 95)
    return {"per_day": [dict(r) for r in per_day], "reasons": [dict(r) for r in reasons],
            "tools": sorted(tools.values(), key=lambda t: -t["calls"])}


@router.get("/analytics", response_class=HTMLResponse)
async def analytics(days: int = 14) -> HTMLResponse:
    days = max(1, min(days, 365))
    d = analytics_data(days)
    peak = max((r["calls"] for r in d["per_day"]), default=1)
    daily = "".join(f"<tr><td>{escape(r['day'] or '?')}</td><td>{r['calls']}</td><td><span class=bar "
                    f"style='width:{max(2, round(200 * r['calls'] / peak))}px'></span></td></tr>"
                    for r in d["per_day"])
    daily = f"<table><tr><th>day</th><th>calls</th><th></th></tr>{daily}</table>" if daily else \
        "<p class=mute>Nothing yet.</p>"
    total = sum(r["calls"] for r in d["per_day"])
    return page(f"Analytics, last {days} days", "".join([
        f"<p>{total} call{'s' if total != 1 else ''}, "
        f"{sum(t['calls'] for t in d['tools'])} tool calls.</p>",
        "<h2>Calls per day</h2>", daily,
        "<h2>Tools</h2>", table(d["tools"], ["tool", "calls", "ok", "error", "confirm", "deferred",
                                              "error_rate", "p50_ms", "p95_ms"]),
        "<h2>How calls ended (all time)</h2>", table(d["reasons"], ["ended_reason", "calls"])]))


_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")  # not allowed in TEXT; tab and newline are


def _ics_text(value: str) -> str:
    """RFC 5545 TEXT escaping. A lone CR would end the content line early in strict parsers."""
    value = _CONTROL.sub("", value.replace("\r\n", "\n").replace("\r", "\n"))
    return (value.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,")
            .replace("\n", "\\n"))


def _fold(line: str) -> str:
    """RFC 5545 lines are at most 75 octets; continuation lines start with a space."""
    out, cur = [], b""
    for ch in line:
        b = ch.encode()
        if len(cur) + len(b) > (75 if not out else 74):
            out.append(cur.decode())
            cur = b""
        cur += b
    out.append(cur.decode())
    return "\r\n ".join(out)


def to_ics(rows: Iterable, tz: str, now: datetime | None = None) -> str:
    """Events (naive local times in tz) as an iCalendar feed with UTC times."""
    stamp = (now or datetime.now(timezone.utc)).strftime("%Y%m%dT%H%M%SZ")
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//voice-agent//calendar//EN",
             "CALSCALE:GREGORIAN", "X-WR-CALNAME:voice-agent"]
    for r in rows:
        start = datetime.fromisoformat(r["starts_at"]).replace(tzinfo=ZoneInfo(tz))
        lines += ["BEGIN:VEVENT", f"UID:event-{r['id']}@voice-agent", f"DTSTAMP:{stamp}",
                  f"DTSTART:{start.astimezone(timezone.utc):%Y%m%dT%H%M%SZ}",
                  f"SUMMARY:{_ics_text(r['title'])}"]
        if r["notes"]:
            lines.append(f"DESCRIPTION:{_ics_text(r['notes'])}")
        lines.append("END:VEVENT")
    lines.append("END:VCALENDAR")
    return "\r\n".join(_fold(line) for line in lines) + "\r\n"


@router.get("/calendar.ics")
async def calendar_ics(caller: str | None = None) -> Response:
    """Every event (or one caller's with ?caller=+1...) for import into a calendar app."""
    sql, args = "SELECT * FROM events", ()
    if caller:
        sql, args = sql + " WHERE caller = ?", (caller,)
    body = to_ics(q(sql + " ORDER BY starts_at", *args), get_settings().timezone)
    return Response(body, media_type="text/calendar; charset=utf-8", headers={
        "Content-Disposition": 'attachment; filename="voice-agent.ics"', "Cache-Control": "no-store"})

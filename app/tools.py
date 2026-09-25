"""Tool registry and dispatcher for Vapi `tool-calls`.

Guards applied in order: unknown tool -> caller allowlist (agentic tools) -> per-caller
rate limit -> required args -> two-step confirmation (side-effecting tools) -> timeout.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime
from zoneinfo import ZoneInfo
from typing import Any, Awaitable, Callable

from . import convert, db, jobs, llm, memory, metrics, notify, reminders, vapi, web_tools
from .config import get_settings
from .logs import log_event, request_id
from .security import limiter

CONFIRM_TTL_S = 300
TOOL_TIMEOUT_S = 18  # Vapi's default server timeout is 20s
STILL_WORKING = "STILL WORKING"
_background: set[asyncio.Task] = set()


@dataclass
class Ctx:
    call_id: str | None
    caller: str | None
    trusted: bool

    @property
    def key(self) -> str:
        return self.caller or self.call_id or "anonymous"


Handler = Callable[[dict, Ctx], Awaitable[str]]


@dataclass
class Tool:
    name: str
    description: str
    properties: dict[str, Any]
    required: list[str]
    handler: Handler
    agentic: bool = False  # needs an allowlisted caller
    confirm: Callable[[dict, Ctx], str] | None = None  # describes the action to confirm
    spoken_start: str = "One moment."
    enabled: Callable[[Any], bool] = lambda s: True  # offered to the model only if true
    # Cached lookup: past TOOL_SOFT_DEADLINE_S the caller hears a holding line, the lookup
    # finishes in the background, and the model's retry is answered from the warm cache.
    deferrable: bool = False

    def schema(self) -> dict:
        props = dict(self.properties)
        if self.confirm:
            props["confirmed"] = {
                "type": "boolean",
                "description": "Set true only after the caller explicitly said yes to the "
                               "confirmation you read back.",
            }
        return {"type": "object", "properties": props, "required": self.required}


# --- handlers -------------------------------------------------------------------------

SUMMARY_SYSTEM = (
    "You summarise web pages for a phone call. Answer in at most three short spoken "
    "sentences, no lists or markdown. If a question is given, answer it from the page only; "
    "say so if the page does not cover it."
)


async def _weather(a: dict, ctx: Ctx) -> str:
    return await web_tools.get_weather(a["location"])


async def _search(a: dict, ctx: Ctx) -> str:
    return web_tools.format_results(await web_tools.search(a["query"]))


async def _news(a: dict, ctx: Ctx) -> str:
    return await web_tools.headlines(a.get("topic") or "top", a.get("query") or "")


async def _wikipedia(a: dict, ctx: Ctx) -> str:
    return await web_tools.wikipedia(a["topic"])


async def _fetch(a: dict, ctx: Ctx) -> str:
    url, text = await web_tools.fetch_text(a["url"])
    if not text:
        return "That page has no readable text."
    question = a.get("question") or "Summarise the page."
    prompt = f"URL: {url}\nQuestion: {question}\n\n<page>\n{text[:40000]}\n</page>"
    summary = await llm.complete(prompt, SUMMARY_SYSTEM, effort="low", max_tokens=600)
    return summary or f"(Summary unavailable) The page begins: {text[:600]}"


async def _convert(a: dict, ctx: Ctx) -> str:
    try:
        amount = float(str(a["amount"]).replace(",", ""))
    except ValueError:
        raise ValueError("amount must be a number.") from None
    return await convert.convert(amount, str(a["from_unit"]), str(a["to_unit"]))


def _owner(ctx: Ctx) -> str:
    return ctx.caller or "web"


async def _add_note(a: dict, ctx: Ctx) -> str:
    with db.connect() as conn:
        nid = conn.execute("INSERT INTO notes (caller, body) VALUES (?, ?)",
                           (_owner(ctx), a["text"])).lastrowid
    return f"Saved note {nid}."


async def _list_notes(a: dict, ctx: Ctx) -> str:
    with db.connect() as conn:
        rows = conn.execute("SELECT id, body, created_at FROM notes WHERE caller = ? "
                            "ORDER BY id DESC LIMIT 5", (_owner(ctx),)).fetchall()
    if not rows:
        return "You have no notes."
    return " ".join(f"Note {r['id']} ({r['created_at'][:10]}): {r['body']}." for r in rows)


async def _forget_me(a: dict, ctx: Ctx) -> str:
    if not ctx.caller:
        raise ValueError("I only keep memories for phone numbers, so there's nothing to forget.")
    n = memory.forget(ctx.caller)
    return (f"Done. I deleted {n} thing{'s' if n != 1 else ''} I remembered about you."
            if n else "I didn't have anything remembered about you.")


def _parse_when(value: str) -> datetime:
    """ISO date-time -> naive local time in TIMEZONE, the form events are stored and compared in.
    A time with an offset ("...T16:00Z", "...+02:00") is converted, not just stripped."""
    try:
        when = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        raise ValueError("starts_at must be an ISO date-time like 2026-09-26T15:00") from None
    if when.tzinfo is not None:
        when = when.astimezone(ZoneInfo(get_settings().timezone)).replace(tzinfo=None)
    return when


async def _create_event(a: dict, ctx: Ctx) -> str:
    when = _parse_when(a["starts_at"])
    with db.connect() as conn:
        conn.execute("INSERT INTO events (caller, title, starts_at, notes) VALUES (?, ?, ?, ?)",
                     (_owner(ctx), a["title"], when.isoformat(timespec="minutes"), a.get("notes")))
    return f"Added {a['title']} on {when:%A %B %d at %H:%M}."


async def _list_events(a: dict, ctx: Ctx) -> str:
    # ponytail: one owner timezone; events are naive local times compared as ISO strings.
    now = datetime.now(ZoneInfo(get_settings().timezone)).replace(tzinfo=None)
    now = now.isoformat(timespec="minutes")
    with db.connect() as conn:
        rows = conn.execute("SELECT title, starts_at FROM events WHERE caller = ? AND starts_at >= ? "
                            "ORDER BY starts_at LIMIT 5", (_owner(ctx), now)).fetchall()
    if not rows:
        return "Nothing upcoming on your calendar."
    return " ".join(f"{r['title']} on {datetime.fromisoformat(r['starts_at']):%A %B %d at %H:%M}."
                    for r in rows)


def _recipient(a: dict, ctx: Ctx) -> str:
    if a.get("channel") == "sms":
        if not ctx.caller:
            raise ValueError("I can only text the number you're calling from, and I don't have it.")
        return ctx.caller  # never text arbitrary numbers
    to = a.get("email") or get_settings().owner_email
    if not to or "@" not in to:
        raise ValueError("I need an email address to send that to.")
    return to


async def _send_followup(a: dict, ctx: Ctx) -> str:
    to = _recipient(a, ctx)
    status = await notify.send(a["channel"], to, "Follow-up from your call", a["message"])
    return f"Done: {status}."


def _reminder_target(a: dict, ctx: Ctx) -> tuple[str, str]:
    if not ctx.caller:
        raise ValueError("I can only set reminders for the number you're calling from, and I don't have it.")
    if a.get("kind") not in ("call", "sms"):
        raise ValueError("kind must be call or sms.")
    return ctx.caller, a["kind"]  # never call or text arbitrary numbers


async def _schedule_reminder(a: dict, ctx: Ctx) -> str:
    to, kind = _reminder_target(a, ctx)
    s = get_settings()
    when = reminders.parse_due(a["when"], s.timezone)
    rid, status = await reminders.schedule(ctx.call_id, to, kind, when, a["message"])
    local = when.astimezone(ZoneInfo(s.timezone))
    how = "call you back" if kind == "call" else "text you"
    dry = status == "dry-run" or (kind == "sms" and not notify.will_send("sms"))
    note = " (dry-run: recorded but nothing will actually be sent)" if dry else ""
    return f"Reminder {rid} set: I'll {how} on {local:%A %B %d at %H:%M}{note}."


def _confirm_reminder(a: dict, ctx: Ctx) -> str:
    to, kind = _reminder_target(a, ctx)
    tz = get_settings().timezone
    local = reminders.parse_due(a["when"], tz).astimezone(ZoneInfo(tz))  # reject bad times up front
    how = "call you back" if kind == "call" else "text you"
    return f"{how} at {to} on {local:%A %B %d at %H:%M} to remind you: {a.get('message')}"


TRANSFER_APPROVAL = "transfer.approved"  # row in `confirmations`, consumed by the webhook


async def _request_transfer(a: dict, ctx: Ctx) -> str:
    if not get_settings().transfer_number:
        raise ValueError("Call transfers aren't set up.")
    if not ctx.call_id:
        raise ValueError("I can't transfer this call.")
    with db.connect() as conn:
        conn.execute("INSERT OR REPLACE INTO confirmations VALUES (?, ?, '', ?)",
                     (ctx.call_id, TRANSFER_APPROVAL, time.time()))
    return "Transfer approved. Tell the caller you're connecting them now, then call transferCall."


def transfer_approved(call_id: str | None) -> bool:
    """Consume a fresh approval recorded by request_transfer for this call."""
    if not call_id:
        return False
    with db.connect() as conn:
        return bool(conn.execute(
            "DELETE FROM confirmations WHERE call_id = ? AND tool = ? AND created_at >= ?",
            (call_id, TRANSFER_APPROVAL, time.time() - CONFIRM_TTL_S)).rowcount)


async def _deep_task(a: dict, ctx: Ctx) -> str:
    s = get_settings()
    to = _recipient(a, ctx)
    if not limiter.allow(f"deep:{ctx.key}", s.deep_tasks_per_hour, 3600):
        raise ValueError("You've hit the hourly limit for deep tasks. Try again later.")
    job_id = jobs.enqueue(ctx.call_id, ctx.caller, a["task"], a["channel"], to)
    return f"Queued task {job_id}. I'll send the results by {a['channel']} to {to} after we hang up."


CHANNEL = {"type": "string", "enum": ["sms", "email"],
           "description": "sms goes to the caller's own number; email to the given address."}
EMAIL = {"type": "string", "description": "Email address (only for channel=email)."}

TOOLS: dict[str, Tool] = {t.name: t for t in [
    Tool("get_weather", "Get current weather and today's forecast for a place.",
         {"location": {"type": "string", "description": "City or place name"}},
         ["location"], _weather, deferrable=True, spoken_start="Checking the weather."),
    Tool("web_search", "Search the web and return the top results with snippets.",
         {"query": {"type": "string"}}, ["query"], _search, deferrable=True,
         spoken_start="Let me look that up."),
    Tool("news_headlines", "Latest news headlines from BBC News, optionally filtered by keywords.",
         {"topic": {"type": "string", "enum": list(web_tools.NEWS_FEEDS)},
          "query": {"type": "string", "description": "Optional keywords that must appear"}},
         [], _news, deferrable=True, spoken_start="Checking the news."),
    Tool("wikipedia", "Look up a person, place, thing or concept on Wikipedia and get the "
         "article's opening summary. Better than web_search for encyclopedic facts.",
         {"topic": {"type": "string"}}, ["topic"], _wikipedia, deferrable=True,
         spoken_start="Looking that up."),
    Tool("fetch_url", "Fetch a public web page and summarise it or answer a question about it.",
         {"url": {"type": "string"}, "question": {"type": "string"}},
         ["url"], _fetch, spoken_start="Reading that page."),
    Tool("convert", "Convert an amount between units (length, mass, volume, speed, "
         "temperature) or between currencies using today's ECB reference rates.",
         {"amount": {"type": "number"},
          "from_unit": {"type": "string", "description": "e.g. km, lb, F, cup, or a currency code like USD"},
          "to_unit": {"type": "string", "description": "e.g. mi, kg, C, ml, or a currency code like EUR"}},
         ["amount", "from_unit", "to_unit"], _convert, deferrable=True,
         spoken_start="Let me work that out."),
    Tool("add_note", "Save a note for the caller.", {"text": {"type": "string"}},
         ["text"], _add_note, agentic=True),
    Tool("list_notes", "Read back the caller's most recent notes.", {}, [], _list_notes,
         agentic=True),
    Tool("forget_me", "Permanently delete everything remembered about the caller from past "
         "calls (long-term memory). Notes and calendar events are not affected.", {}, [],
         _forget_me, agentic=True,
         confirm=lambda a, c: "permanently delete everything I remember about you from past calls"),
    Tool("create_event", "Add an event to the caller's calendar.",
         {"title": {"type": "string"},
          "starts_at": {"type": "string", "description": "ISO 8601 local date-time"},
          "notes": {"type": "string"}},
         ["title", "starts_at"], _create_event, agentic=True,
         confirm=lambda a, c: f"add '{a.get('title')}' to the calendar at {a.get('starts_at')}"),
    Tool("list_events", "List the caller's upcoming calendar events.", {}, [], _list_events,
         agentic=True),
    Tool("send_followup", "Send the caller a follow-up message by SMS or email.",
         {"channel": CHANNEL, "message": {"type": "string"}, "email": EMAIL},
         ["channel", "message"], _send_followup, agentic=True,
         confirm=lambda a, c: f"send this {a.get('channel')} to {_recipient(a, c)}: {a.get('message')}"),
    Tool("schedule_reminder",
         "Schedule a reminder for the caller: a phone call back or an SMS at a given time. "
         "Only ever goes to the number they are calling from.",
         {"kind": {"type": "string", "enum": ["call", "sms"]},
          "when": {"type": "string", "description": "ISO 8601 local date-time, e.g. 2026-09-26T15:00"},
          "message": {"type": "string", "description": "What to remind them about"}},
         ["kind", "when", "message"], _schedule_reminder, agentic=True,
         spoken_start="Setting that reminder.", confirm=_confirm_reminder),
    Tool("request_transfer",
         "Ask to transfer the caller to the owner's phone. Must succeed before transferCall.",
         {"reason": {"type": "string", "description": "Why they want to be transferred"}}, [],
         _request_transfer, agentic=True,
         enabled=lambda s: bool(s.transfer_number),
         confirm=lambda a, c: "transfer this call to the owner's phone"),
    Tool("deep_task",
         "Queue a longer research or writing task that runs after the call; results are "
         "sent by SMS or email. Use when an answer needs more than a few seconds of work.",
         {"task": {"type": "string", "description": "Full, self-contained task description"},
          "channel": CHANNEL, "email": EMAIL},
         ["task", "channel"], _deep_task, agentic=True, spoken_start="Setting that up.",
         confirm=lambda a, c: f"work on '{a.get('task')}' and send results by "
                              f"{a.get('channel')} to {_recipient(a, c)}"),
]}


# --- dispatch -------------------------------------------------------------------------

def _needs_confirmation(call: vapi.ToolCall, ctx: Ctx) -> bool:
    """First call records a pending action; a repeat with confirmed=true consumes it.

    The model cannot skip the read-back by sending confirmed=true on the first call.
    """
    args = {k: v for k, v in call.args.items() if k != "confirmed"}
    digest = hashlib.sha256(json.dumps(args, sort_keys=True, default=str).encode()).hexdigest()
    scope = ctx.call_id or ctx.key
    confirmed = call.args.get("confirmed") in (True, "true", "True")
    with db.connect() as conn:
        conn.execute("DELETE FROM confirmations WHERE created_at < ?", (time.time() - CONFIRM_TTL_S,))
        if confirmed and conn.execute(
            "DELETE FROM confirmations WHERE call_id = ? AND tool = ? AND args_hash = ?",
            (scope, call.name, digest),
        ).rowcount:
            return False
        conn.execute("INSERT OR REPLACE INTO confirmations VALUES (?, ?, ?, ?)",
                     (scope, call.name, digest, time.time()))
    return True


tool_calls_total = metrics.Counter("voice_agent_tool_calls_total",
                                   "Tool calls by tool and outcome (ok, error, confirm, deferred).",
                                   ("tool", "outcome"))
tool_latency = metrics.Histogram("voice_agent_tool_duration_seconds",
                                 "Tool call latency including guards.", ("tool",))


def _outcome(out: dict) -> str:
    if "error" in out:
        return "error"
    if out["result"].startswith(STILL_WORKING):
        return "deferred"
    return "confirm" if out["result"].startswith("CONFIRMATION REQUIRED") else "ok"


async def run(call: vapi.ToolCall, ctx: Ctx) -> dict:
    start = time.perf_counter()
    out = await _dispatch(call, ctx)
    elapsed = time.perf_counter() - start
    name = call.name if call.name in TOOLS else "unknown"  # bounded label values
    outcome = _outcome(out)
    tool_calls_total.inc(tool=name, outcome=outcome)
    tool_latency.observe(elapsed, tool=name)
    _record(call, ctx, outcome, out.get("result", out.get("error")), elapsed)
    return out


def _record(call: vapi.ToolCall, ctx: Ctx, outcome: str, output: str, elapsed: float) -> None:
    """Keep an audit trail of tool calls for the admin dashboard."""
    try:
        with db.connect() as conn:
            conn.execute(
                "INSERT INTO tool_calls (call_id, caller, tool, args, outcome, output, ms, request_id) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (ctx.call_id, ctx.caller, call.name[:100], json.dumps(call.args, default=str)[:4000],
                 outcome, output, round(elapsed * 1000), request_id.get()))
    except Exception as e:  # never fail a live call because the audit write failed
        log_event("tool.record_failed", logging.ERROR, error=repr(e))


def _defer(call: vapi.ToolCall, ctx: Ctx, task: asyncio.Task) -> dict:
    """Answer now with a holding instruction; let the lookup finish and fill the cache."""
    _background.add(task)
    task.add_done_callback(_background.discard)
    task.add_done_callback(lambda t: t.cancelled() or t.exception())  # no "never retrieved"
    log_event("tool.deferred", tool=call.name, call_id=ctx.call_id)
    return vapi.result(call, f"{STILL_WORKING}. The lookup is slow right now. Tell the caller "
                             f"you're still checking, then call {call.name} again with the same "
                             "arguments; the answer will be ready.")


async def _dispatch(call: vapi.ToolCall, ctx: Ctx) -> dict:
    s = get_settings()
    tool = TOOLS.get(call.name)
    if tool is None:
        return vapi.error(call, f"Unknown tool {call.name}.")
    if tool.agentic and not ctx.trusted:
        log_event("tool.denied", logging.WARNING, tool=call.name, caller=ctx.caller, call_id=ctx.call_id)
        return vapi.error(call, "That action isn't available for this caller.")
    if not limiter.allow(f"tools:{ctx.key}", s.rate_limit_per_minute, 60):
        return vapi.error(call, "Too many requests. Please wait a minute and try again.")
    missing = [r for r in tool.required if not str(call.args.get(r) or "").strip()]
    if missing:
        return vapi.error(call, f"Missing required argument(s): {', '.join(missing)}.")

    start = time.perf_counter()
    try:
        if tool.confirm and _needs_confirmation(call, ctx):
            action = tool.confirm(call.args, ctx).rstrip(". ")
            log_event("tool.confirmation_requested", tool=call.name, call_id=ctx.call_id)
            return vapi.result(call, f"CONFIRMATION REQUIRED. Read this back and ask the caller to "
                                     f"confirm: I'm about to {action}. If they clearly say yes, call "
                                     f"{call.name} again with the same arguments and confirmed=true.")
        task = asyncio.ensure_future(tool.handler(call.args, ctx))
        if tool.deferrable and s.tool_soft_deadline_s > 0:
            done, _ = await asyncio.wait({task}, timeout=s.tool_soft_deadline_s)
            if not done:
                return _defer(call, ctx, task)
        out = await asyncio.wait_for(task, TOOL_TIMEOUT_S)
    except ValueError as e:
        return vapi.error(call, str(e))
    except asyncio.TimeoutError:
        log_event("tool.timeout", logging.WARNING, tool=call.name, call_id=ctx.call_id)
        return vapi.error(call, "That took too long. Offer to run it as a deep task instead.")
    except Exception as e:
        log_event("tool.failed", logging.ERROR, tool=call.name, call_id=ctx.call_id, error=repr(e))
        return vapi.error(call, f"The {call.name} tool failed. Tell the caller and move on.")
    log_event("tool.done", tool=call.name, call_id=ctx.call_id,
              ms=round((time.perf_counter() - start) * 1000))
    return vapi.result(call, out)

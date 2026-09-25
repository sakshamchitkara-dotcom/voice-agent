"""Per-caller long-term memory: short facts extracted from end-of-call reports.

Facts are keyed by the caller's phone number and injected into the system prompt of that
caller's next assistant-request. Only allowlisted callers get memory (caller ID can be
spoofed, so memory is private data like notes).
"""
from __future__ import annotations

import json
import re

from . import db, llm
from .logs import log_event

MAX_FACTS_PER_CALLER = 50
_SENSITIVE = re.compile(r"\d[\d\s-]{5,}\d|password|passcode|\bpin\b|social security|\bssn\b|cvv", re.I)
_END = r"([^.!?;]{2,60})"

# (pattern, template, title-case the last value). Deliberately conservative: a missed fact is
# cheap, a wrong "memory" read back to the caller is not.
RULES: list[tuple[re.Pattern, str, bool]] = [(re.compile(p, re.I), t, title) for p, t, title in [
    (r"\bmy name is ([a-z][a-z'-]+(?: [a-z][a-z'-]+)?)", "Their name is {0}.", True),
    (r"\bplease call me ([a-z][a-z'-]+)", "They like to be called {0}.", True),
    (r"\bi live in " + _END, "They live in {0}.", False),
    (r"\bi(?:'m| am) (?:based|located) in " + _END, "They are based in {0}.", False),
    (r"\bi work (?:at|for) " + _END, "They work at {0}.", False),
    # Needs "'s name is" / "is called" / "is named": "my sister is visiting" is not a name.
    (r"\bmy (wife|husband|partner|son|daughter|mom|mum|dad|brother|sister|boss|dog|cat)"
     r"(?:'s name is| is called| is named) ([a-z][a-z'-]+)", "Their {0} is {1}.", True),
    (r"\bmy favou?rite ([a-z]+(?: [a-z]+)?) is " + _END, "Their favourite {0} is {1}.", False),
    (r"\bi prefer " + _END, "They prefer {0}.", False),
    (r"\bi(?:'m| am) allergic to " + _END, "They are allergic to {0}.", False),
    (r"\bremember (?:that )?([^.!?]{3,120})", "They asked you to remember: {0}.", False),
]]


def user_lines(message: dict) -> list[str]:
    """What the caller said, from artifact.messages or else the flat transcript."""
    artifact = message.get("artifact") or {}
    msgs = artifact.get("messages") or message.get("messages") or []
    lines = [m.get("message") or m.get("content") or "" for m in msgs if m.get("role") == "user"]
    if lines:
        return [line for line in lines if line.strip()]
    transcript = artifact.get("transcript") or message.get("transcript") or ""
    # Vapi's flat transcript: "AI: ... User: ..." (sometimes newline separated).
    parts = re.split(r"\b(AI|User|Assistant|Bot):\s*", transcript)
    return [parts[i + 1].strip() for i in range(1, len(parts) - 1, 2)
            if parts[i] == "User" and parts[i + 1].strip()]


# First person -> third person, so "remember that my passport..." reads "their passport".
_PRONOUNS = [(r"\bI am\b|\bI'm\b", "they're"), (r"\bI\b", "they"), (r"\bmy\b", "their"),
             (r"\bme\b", "them"), (r"\bmine\b", "theirs"), (r"\bmyself\b", "themselves")]


def _clean(value: str) -> str:
    value = " ".join(value.split()).strip(" ,'\"")
    value = re.sub(r"\s+(?:and|but|so|because)$", "", value, flags=re.I)
    for pattern, repl in _PRONOUNS:
        value = re.sub(pattern, repl, value, flags=re.I)
    return value


def rule_facts(lines: list[str]) -> list[str]:
    facts = []
    for line in lines:
        for pattern, template, title in RULES:
            for m in pattern.finditer(line):
                groups = [_clean(g) for g in m.groups()]
                if title:
                    groups[-1] = groups[-1].title()
                facts.append(template.format(*groups))
    return facts


def acceptable(fact: str) -> bool:
    return 3 < len(fact) <= 200 and not _SENSITIVE.search(fact)


def save(caller: str, facts: list[str], call_id: str | None, source: str) -> int:
    """Store new facts (deduplicated, case-insensitive) and trim to the newest N. Returns #added."""
    added = 0
    with db.connect() as conn:
        known = {r[0].lower() for r in conn.execute("SELECT fact FROM memories WHERE caller = ?", (caller,))}
        for fact in dict.fromkeys(" ".join(f.split()) for f in facts):
            if acceptable(fact) and fact.lower() not in known:
                conn.execute("INSERT INTO memories (caller, fact, source, call_id) VALUES (?, ?, ?, ?)",
                             (caller, fact, source, call_id))
                known.add(fact.lower())
                added += 1
        conn.execute("DELETE FROM memories WHERE caller = ? AND id NOT IN (SELECT id FROM memories "
                      "WHERE caller = ? ORDER BY id DESC LIMIT ?)", (caller, caller, MAX_FACTS_PER_CALLER))
    return added


def facts_for(caller: str | None, limit: int = 20) -> list[str]:
    if not caller:
        return []
    with db.connect() as conn:
        rows = conn.execute("SELECT fact FROM memories WHERE caller = ? ORDER BY id DESC LIMIT ?",
                            (caller, limit)).fetchall()
    return [r[0] for r in reversed(rows)]


def forget(caller: str) -> int:
    with db.connect() as conn:
        return conn.execute("DELETE FROM memories WHERE caller = ?", (caller,)).rowcount


EXTRACT_SYSTEM = (
    "You maintain long-term memory for a personal phone assistant. From what the CALLER said "
    "in this call, extract durable facts worth knowing on future calls: their name, people in "
    "their life, where they live or work, preferences, ongoing plans or projects, and anything "
    "they explicitly asked you to remember. Skip one-off requests (weather, searches), anything "
    "the assistant said, facts already known, and secrets (passwords, card or account numbers). "
    "Write each fact as a short third-person sentence, e.g. \"Their daughter Mia starts school "
    "in October.\" Reply with only a JSON array of strings; [] if there is nothing new."
)


def _parse_json_list(text: str) -> list[str] | None:
    m = re.search(r"\[.*\]", text, re.S)
    try:
        data = json.loads(m.group(0)) if m else None
    except json.JSONDecodeError:
        return None
    return [str(x) for x in data if isinstance(x, str)] if isinstance(data, list) else None


async def claude_facts(lines: list[str], known: list[str]) -> list[str] | None:
    """Facts via Claude, or None when Claude is unavailable or replies with junk."""
    prompt = ("Already known:\n" + ("\n".join(f"- {k}" for k in known) or "(nothing)")
              + "\n\nWhat the caller said, in order:\n" + "\n".join(f"- {line}" for line in lines))
    text = await llm.complete(prompt[:50_000], EXTRACT_SYSTEM, effort="low", max_tokens=800)
    return _parse_json_list(text) if text else None


async def remember_call(caller: str, call_id: str | None, message: dict) -> int:
    """Extract facts from an end-of-call report and store them. Returns how many were new."""
    lines = user_lines(message)
    if not lines:
        return 0
    facts, source = await claude_facts(lines, facts_for(caller, MAX_FACTS_PER_CALLER)), "claude"
    if facts is None:
        facts, source = rule_facts(lines), "rules"
    added = save(caller, facts, call_id, source)
    log_event("memory.updated", call_id=call_id, source=source, extracted=len(facts), added=added)
    return added

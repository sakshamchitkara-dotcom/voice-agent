"""Per-caller long-term memory: short facts extracted from end-of-call reports.

Facts are keyed by the caller's phone number and injected into the system prompt of that
caller's next assistant-request. Only allowlisted callers get memory (caller ID can be
spoofed, so memory is private data like notes).
"""
from __future__ import annotations

import re

from . import db

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
    (r"\bmy (wife|husband|partner|son|daughter|mom|mum|dad|brother|sister|boss|dog|cat)(?:'s name)? is "
     r"(?:called |named )?([a-z][a-z'-]+)", "Their {0} is {1}.", True),
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


def _clean(value: str) -> str:
    value = " ".join(value.split()).strip(" ,'\"")
    return re.sub(r"\s+(?:and|but|so|because)$", "", value, flags=re.I)


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

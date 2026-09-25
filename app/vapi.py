"""Helpers for Vapi server-message payloads (docs.vapi.ai/server-url/events)."""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any


@dataclass
class ToolCall:
    id: str
    name: str
    args: dict[str, Any]


def _args(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str) and raw.strip():
        try:
            parsed = json.loads(raw)
            return parsed if isinstance(parsed, dict) else {}
        except json.JSONDecodeError:
            return {}
    return {}


def tool_calls(message: dict) -> list[ToolCall]:
    """Normalise `toolCallList`.

    Vapi documents two shapes for the same list:
      {"id", "name", "parameters"}                       (server events page)
      {"id", "type": "function", "function": {"name", "arguments"}}  (custom tools page)
    `arguments` may be an object or a JSON string.
    """
    out = []
    for tc in message.get("toolCallList") or []:
        fn = tc.get("function") or {}
        name = tc.get("name") or fn.get("name") or ""
        raw = tc.get("parameters") if "parameters" in tc else fn.get("arguments", tc.get("arguments"))
        out.append(ToolCall(id=tc.get("id", ""), name=name, args=_args(raw)))
    return out


def one_line(text: Any, limit: int = 1800) -> str:
    """Tool results must be a single-line string."""
    s = " ".join(str(text).split())
    return s if len(s) <= limit else s[: limit - 1] + "…"


def result(call: ToolCall, text: Any) -> dict:
    return {"name": call.name, "toolCallId": call.id, "result": one_line(text)}


def error(call: ToolCall, text: Any) -> dict:
    return {"name": call.name, "toolCallId": call.id, "error": one_line(text)}


def call_of(message: dict) -> dict:
    return message.get("call") or {}


def caller_number(message: dict) -> str | None:
    customer = message.get("customer") or call_of(message).get("customer") or {}
    return customer.get("number")


def call_type(message: dict) -> str | None:
    return call_of(message).get("type")

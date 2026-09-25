"""Build the Vapi assistant config, per caller for `assistant-request`, or for the saved assistant."""
from __future__ import annotations

from .config import Settings
from .tools import TOOLS

SYSTEM_PROMPT = """You are {owner} personal voice assistant, speaking on a phone call.

Now: {{{{"now" | date: "%A, %B %d, %Y, %H:%M", "{tz}"}}}} ({tz}).

Style: talk like a sharp, friendly human. Short sentences, no lists, no markdown, no URLs read
aloud unless asked. Ask one clarifying question when a request is ambiguous.

Tools:
- Use get_weather, web_search and fetch_url for live facts; never invent current information.
- {agentic}
- Some tools return "CONFIRMATION REQUIRED". Read the action back in plain words, wait for a clear
  yes, then call the same tool again with identical arguments plus confirmed=true. If the caller
  hesitates or changes anything, do not confirm.
- If a tool errors, say so briefly and offer an alternative.
- For anything needing research or more than a few seconds of work, offer deep_task: it runs
  after the call and the result is sent by SMS or email.
- Calendar times are ISO 8601 local times in {tz}, e.g. 2026-09-26T15:00."""

AGENTIC_ON = ("You may take notes, manage the calendar, send follow-ups and queue deep tasks "
              "for this caller.")
AGENTIC_OFF = ("This caller is not on the allowlist: you can only answer questions, search the "
               "web and check the weather. Politely decline notes, calendar, messages and tasks.")

SERVER_MESSAGES = ["tool-calls", "status-update", "end-of-call-report"]


def server_block(s: Settings) -> dict:
    server: dict = {"url": f"{s.public_url}/vapi/webhook", "timeoutSeconds": 20}
    if s.vapi_credential_id:
        server["credentialId"] = s.vapi_credential_id
    elif s.webhook_secret:
        server["headers"] = {"X-Vapi-Secret": s.webhook_secret}
    return server


def tool_defs(s: Settings, trusted: bool) -> list[dict]:
    server = server_block(s)
    return [
        {
            "type": "function",
            "function": {"name": t.name, "description": t.description, "parameters": t.schema()},
            "server": server,
            "messages": [{"type": "request-start", "content": t.spoken_start}],
        }
        for t in TOOLS.values()
        if trusted or not t.agentic
    ]


def build_assistant(s: Settings, trusted: bool, name: str = "Voice Agent") -> dict:
    owner = f"{s.owner_name}'s" if s.owner_name else "a"
    hey = f"Hey {s.owner_name}," if s.owner_name else "Hey,"
    prompt = SYSTEM_PROMPT.format(owner=owner, tz=s.timezone,
                                  agentic=AGENTIC_ON if trusted else AGENTIC_OFF)
    assistant: dict = {
        "name": name,
        "firstMessage": (f"{hey} what can I do for you?" if trusted
                         else "Hi, you've reached a personal assistant. How can I help?"),
        "model": {
            "provider": s.vapi_llm_provider,
            "model": s.vapi_llm_model,
            "messages": [{"role": "system", "content": prompt}],
            "tools": tool_defs(s, trusted),
        },
        "server": server_block(s),
        "serverMessages": SERVER_MESSAGES,
        "metadata": {"trusted": trusted},
    }
    if s.vapi_voice_provider and s.vapi_voice_id:
        assistant["voice"] = {"provider": s.vapi_voice_provider, "voiceId": s.vapi_voice_id}
    return assistant

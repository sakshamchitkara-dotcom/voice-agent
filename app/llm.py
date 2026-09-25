"""Claude as the reasoning backend, with model fallback and a no-LLM escape hatch.

complete() returns None when Claude is unavailable (no credentials, outage, refusal on
every model) so callers can degrade to a non-LLM answer instead of failing the call.
"""
from __future__ import annotations

import logging
from typing import Any

import anthropic

from .config import get_settings
from .logs import log_event

WEB_SEARCH_TOOL = {"type": "web_search_20260209", "name": "web_search", "max_uses": 5}
_client: anthropic.AsyncAnthropic | None = None


def _get_client() -> anthropic.AsyncAnthropic:
    global _client
    if _client is None:
        _client = anthropic.AsyncAnthropic(timeout=120.0, max_retries=1)
    return _client


def _text(content: list[Any]) -> str:
    return "\n".join(b.text for b in content if b.type == "text").strip()


async def complete(
    prompt: str,
    system: str,
    *,
    effort: str = "low",
    max_tokens: int = 2000,
    web_search: bool = False,
) -> str | None:
    s = get_settings()
    models = [m for m in (s.claude_model, s.claude_fallback_model) if m]
    for model in dict.fromkeys(models):
        try:
            text = await _run(model, prompt, system, effort, max_tokens, web_search)
        except TypeError as e:  # SDK raises TypeError when no credentials resolve
            log_event("llm.no_credentials", logging.WARNING, error=str(e)[:120])
            return None
        except anthropic.AuthenticationError as e:
            log_event("llm.auth_failed", logging.ERROR, model=model, error=e.message)
            return None
        except (anthropic.NotFoundError, anthropic.BadRequestError) as e:
            log_event("llm.model_rejected", logging.WARNING, model=model, error=e.message)
            continue
        except anthropic.RateLimitError:
            log_event("llm.rate_limited", logging.WARNING, model=model)
            continue
        except anthropic.APIStatusError as e:
            log_event("llm.api_error", logging.WARNING, model=model, status=e.status_code)
            continue
        except anthropic.APIConnectionError as e:
            log_event("llm.connection_error", logging.WARNING, model=model, error=str(e))
            continue
        if text:
            return text
    return None


async def _run(model, prompt, system, effort, max_tokens, web_search) -> str | None:
    client = _get_client()
    messages: list[dict] = [{"role": "user", "content": prompt}]
    kwargs: dict[str, Any] = dict(
        model=model,
        max_tokens=max_tokens,
        system=system,
        output_config={"effort": effort},
    )
    if web_search:
        kwargs["tools"] = [WEB_SEARCH_TOOL]
    # Server-side web search may pause a long turn; resume a few times.
    for _ in range(4):
        resp = await client.messages.create(messages=messages, **kwargs)
        if resp.stop_reason == "refusal":
            log_event("llm.refusal", logging.WARNING, model=model)
            return None
        if resp.stop_reason != "pause_turn":
            log_event("llm.done", model=model, stop=resp.stop_reason,
                      in_tokens=resp.usage.input_tokens, out_tokens=resp.usage.output_tokens)
            return _text(resp.content)
        messages = [*messages, {"role": "assistant", "content": resp.content}]
    return _text(resp.content)

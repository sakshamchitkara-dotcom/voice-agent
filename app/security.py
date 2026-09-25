"""Webhook authentication, caller allowlist and rate limiting."""
from __future__ import annotations

import hashlib
import hmac
import time
from collections import defaultdict, deque
from typing import Mapping

from .config import Settings


def verify_webhook(headers: Mapping[str, str], body: bytes, s: Settings) -> bool:
    """Check the request against every configured Vapi auth method.

    Vapi server auth options (docs.vapi.ai/server-url/server-authentication):
    - legacy `X-Vapi-Secret: <secret>` header
    - Bearer credential: `Authorization: Bearer <secret>`
    - HMAC credential: signature of the body in a configurable header
    Headers must be a case-insensitive mapping (Starlette's Headers is).
    """
    if not s.webhook_secret and not s.hmac_secret:
        return s.allow_unauthenticated

    if s.webhook_secret:
        token = headers.get("x-vapi-secret", "")
        auth = headers.get("authorization", "")
        if not token and auth.lower().startswith("bearer "):
            token = auth[7:].strip()
        if not hmac.compare_digest(token.encode(), s.webhook_secret.encode()):
            return False

    if s.hmac_secret:
        sig = headers.get(s.hmac_header, "").strip()
        sig = sig.split("=", 1)[1] if sig.startswith("sha256=") else sig
        expected = hmac.new(s.hmac_secret.encode(), body, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(sig.lower().encode(), expected.encode()):
            return False

    return True


def is_trusted(caller: str | None, call_type: str | None, s: Settings) -> bool:
    """Trusted callers may use agentic tools (side effects, private data)."""
    if call_type == "webCall":
        return s.allow_web_agentic
    return bool(caller) and caller in s.allowed_callers


class RateLimiter:
    """Sliding-window limiter keyed by caller.

    ponytail: in-process memory; use Redis if you run more than one instance.
    """

    def __init__(self) -> None:
        self._hits: dict[str, deque[float]] = defaultdict(deque)

    def allow(self, key: str, limit: int, window_s: float) -> bool:
        now = time.monotonic()
        hits = self._hits[key]
        while hits and now - hits[0] > window_s:
            hits.popleft()
        if len(hits) >= limit:
            return False
        hits.append(now)
        return True


limiter = RateLimiter()

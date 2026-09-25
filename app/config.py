"""Settings read from the environment. Call get_settings(); tests clear the cache."""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from functools import lru_cache


def _bool(name: str, default: bool = False) -> bool:
    return os.getenv(name, str(default)).strip().lower() in {"1", "true", "yes", "on"}


def _list(name: str) -> tuple[str, ...]:
    return tuple(x.strip() for x in os.getenv(name, "").split(",") if x.strip())


def _e164(value: str) -> str:
    value = value.strip()
    if value and not re.fullmatch(r"\+[1-9]\d{6,14}", value):
        raise ValueError(f"TRANSFER_NUMBER must be E.164 like +14155550123, got {value!r}")
    return value


@dataclass(frozen=True)
class Settings:
    public_url: str
    # Webhook auth: shared secret (X-Vapi-Secret or Authorization: Bearer) and/or HMAC.
    webhook_secret: str
    hmac_secret: str
    hmac_header: str
    allow_unauthenticated: bool
    db_path: str
    # Callers (E.164) allowed to use side-effecting / private tools.
    allowed_callers: tuple[str, ...]
    allow_web_agentic: bool
    rate_limit_per_minute: int
    # Cached lookups slower than this answer with a holding line and finish in the background.
    tool_soft_deadline_s: float
    # Places whose weather is fetched at startup and kept warm (e.g. the owner's city).
    weather_prefetch: tuple[str, ...]
    deep_tasks_per_hour: int
    # Claude reasoning backend for tools and deep tasks.
    claude_model: str
    claude_fallback_model: str
    # Conversational LLM that Vapi runs during the call (must be in Vapi's enum).
    vapi_llm_provider: str
    vapi_llm_model: str
    vapi_voice_provider: str
    vapi_voice_id: str
    vapi_credential_id: str
    # Private API key and number, used for scheduled reminder callbacks (POST /call).
    vapi_api_key: str
    vapi_phone_number_id: str
    owner_name: str
    # E.164 number allowlisted callers can be transferred to (Vapi transferCall). Empty = off.
    transfer_number: str
    timezone: str
    owner_email: str
    # Admin dashboard (HTTP Basic). Disabled while ADMIN_PASSWORD is empty.
    admin_user: str
    admin_password: str
    # Notifications. Nothing is sent unless DRY_RUN=false AND the channel is configured.
    dry_run: bool
    smtp_host: str
    smtp_port: int
    smtp_user: str
    smtp_password: str
    smtp_from: str
    twilio_account_sid: str
    twilio_auth_token: str
    twilio_from: str


def load_settings() -> Settings:
    e = os.getenv
    return Settings(
        public_url=e("PUBLIC_URL", "http://localhost:8000").rstrip("/"),
        webhook_secret=e("VAPI_WEBHOOK_SECRET", ""),
        hmac_secret=e("VAPI_HMAC_SECRET", ""),
        hmac_header=e("VAPI_HMAC_HEADER", "x-vapi-signature").lower(),
        allow_unauthenticated=_bool("ALLOW_UNAUTHENTICATED"),
        db_path=e("DB_PATH", "data/voice_agent.db"),
        allowed_callers=_list("ALLOWED_CALLERS"),
        allow_web_agentic=_bool("ALLOW_WEB_AGENTIC"),
        rate_limit_per_minute=int(e("RATE_LIMIT_PER_MINUTE", "20")),
        tool_soft_deadline_s=float(e("TOOL_SOFT_DEADLINE_S", "1.5")),
        weather_prefetch=tuple(x.strip() for x in e("WEATHER_PREFETCH", "").split(";") if x.strip()),
        deep_tasks_per_hour=int(e("DEEP_TASKS_PER_HOUR", "3")),
        claude_model=e("CLAUDE_MODEL", "claude-opus-5-5"),
        claude_fallback_model=e("CLAUDE_FALLBACK_MODEL", "claude-opus-5"),
        vapi_llm_provider=e("VAPI_LLM_PROVIDER", "anthropic"),
        vapi_llm_model=e("VAPI_LLM_MODEL", "claude-sonnet-5"),
        vapi_voice_provider=e("VAPI_VOICE_PROVIDER", ""),
        vapi_voice_id=e("VAPI_VOICE_ID", ""),
        vapi_credential_id=e("VAPI_CREDENTIAL_ID", ""),
        vapi_api_key=e("VAPI_API_KEY", ""),
        vapi_phone_number_id=e("VAPI_PHONE_NUMBER_ID", ""),
        owner_name=e("OWNER_NAME", ""),
        transfer_number=_e164(e("TRANSFER_NUMBER", "")),
        timezone=e("TIMEZONE", "UTC"),
        owner_email=e("OWNER_EMAIL", ""),
        admin_user=e("ADMIN_USER", "admin"),
        admin_password=e("ADMIN_PASSWORD", ""),
        dry_run=_bool("DRY_RUN", True),
        smtp_host=e("SMTP_HOST", ""),
        smtp_port=int(e("SMTP_PORT", "587")),
        smtp_user=e("SMTP_USER", ""),
        smtp_password=e("SMTP_PASSWORD", ""),
        smtp_from=e("SMTP_FROM", ""),
        twilio_account_sid=e("TWILIO_ACCOUNT_SID", ""),
        twilio_auth_token=e("TWILIO_AUTH_TOKEN", ""),
        twilio_from=e("TWILIO_FROM", ""),
    )


@lru_cache
def get_settings() -> Settings:
    return load_settings()

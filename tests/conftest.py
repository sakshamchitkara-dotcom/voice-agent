import json
from pathlib import Path

import pytest

from app import db, web_tools
from app.config import get_settings

FIXTURES = Path(__file__).parent / "fixtures"


def load_fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


@pytest.fixture(autouse=True)
def env(tmp_path, monkeypatch):
    """Fresh sqlite db and deterministic settings for every test."""
    monkeypatch.setenv("DB_PATH", str(tmp_path / "test.db"))
    monkeypatch.setenv("VAPI_WEBHOOK_SECRET", "test-secret")
    monkeypatch.setenv("ALLOWED_CALLERS", "+14155550100")
    monkeypatch.setenv("PUBLIC_URL", "https://agent.example.com")
    monkeypatch.setenv("DRY_RUN", "true")
    for var in ("VAPI_HMAC_SECRET", "ANTHROPIC_API_KEY", "SMTP_HOST", "TWILIO_ACCOUNT_SID"):
        monkeypatch.delenv(var, raising=False)
    get_settings.cache_clear()
    web_tools.clear_caches()
    db.init_db()
    yield
    get_settings.cache_clear()

from app import db, notify
from app.config import get_settings


def outbox():
    with db.connect() as conn:
        return [dict(r) for r in conn.execute("SELECT channel, recipient, status FROM outbox")]


async def test_dry_run_by_default_records_outbox():
    out = await notify.send("sms", "+14155550100", "Call summary", "hello")
    assert out.startswith("dry-run")
    assert outbox() == [{"channel": "sms", "recipient": "+14155550100", "status": "dry-run"}]


async def test_live_mode_without_config_still_dry_runs(monkeypatch):
    monkeypatch.setenv("DRY_RUN", "false")
    get_settings.cache_clear()
    assert (await notify.send("email", "a@b.co", "s", "b")).startswith("dry-run")


async def test_live_email_uses_smtp(monkeypatch):
    monkeypatch.setenv("DRY_RUN", "false")
    monkeypatch.setenv("SMTP_HOST", "smtp.example.com")
    get_settings.cache_clear()
    sent = []
    monkeypatch.setattr(notify, "_smtp_send", lambda s, to, subj, body: sent.append((to, subj)))
    assert await notify.send("email", "a@b.co", "Subj", "Body") == "email sent to a@b.co"
    assert sent == [("a@b.co", "Subj")] and outbox()[0]["status"] == "sent"

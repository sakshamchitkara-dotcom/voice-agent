from fastapi.testclient import TestClient

from app import db, postcall
from app.main import app
from tests.conftest import load_fixture


def outbox():
    with db.connect() as conn:
        return [dict(r) for r in conn.execute("SELECT channel, recipient, subject, body, status FROM outbox")]


def test_end_of_call_emails_owner_a_summary(monkeypatch):
    from app.config import get_settings
    monkeypatch.setenv("POST_CALL_EMAIL", "true")
    monkeypatch.setenv("OWNER_EMAIL", "sam@example.com")
    get_settings.cache_clear()
    with db.connect() as conn:
        for tool in ("get_weather", "get_weather", "add_note"):
            conn.execute("INSERT INTO tool_calls (call_id, tool, outcome) VALUES ('call-0001', ?, 'ok')", (tool,))
        conn.execute("INSERT INTO reminders (call_id, caller, kind, due_at, message, status) VALUES "
                     "('call-0001', '+14155550100', 'sms', '2026-09-26T22:00:00+00:00', 'stretch', 'pending')")
    with TestClient(app) as c:
        c.post("/vapi/webhook", json=load_fixture("end_of_call_report.json"), headers={"X-Vapi-Secret": "test-secret"})
    [mail] = outbox()
    assert mail["recipient"] == "sam@example.com" and mail["status"] == "dry-run"
    assert mail["subject"] == "Call from +14155550100 (3 min 12 s)"
    body = mail["body"]
    assert "The caller asked about the weather in San Francisco" in body
    assert "- add_note\n- get_weather ×2" in body
    assert "- Reminder (sms) at 2026-09-26T22:00:00+00:00: stretch" in body
    assert body.rstrip().endswith("https://agent.example.com/admin/calls/call-0001")


async def test_off_by_default_and_unknown_calls_render_nothing():
    db.upsert_call("c1", caller="+1", summary="hi")
    assert await postcall.email_owner("c1") is None and outbox() == []
    assert postcall.render("nope") is None
    assert postcall._length(None, "2026-09-25T17:00:00Z") == "unknown"


def test_outbound_callback_is_not_described_as_the_caller_calling():
    db.upsert_call("vapi-call-9", caller="+14155550100", type="outboundPhoneCall", ended_reason="voicemail",
                   started_at="2026-09-26T22:00:02.000Z", ended_at="2026-09-26T22:00:41.000Z")
    subject, body = postcall.render("vapi-call-9")
    assert subject == "Call to +14155550100 (0 min 39 s)"
    assert body.startswith("Outbound call to +14155550100 at 2026-09-26T22:00:02.000Z.\nLength: 0 min 39 s · Ended: voicemail")

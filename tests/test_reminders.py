import functools
import json
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from app import db, reminders, tools
from app.config import get_settings
from app.security import limiter
from app.vapi import ToolCall

TRUSTED = tools.Ctx(call_id="call-1", caller="+14155550100", trusted=True)


@pytest.fixture(autouse=True)
def fresh_limiter():
    limiter._hits.clear()


def soon(hours=2) -> str:
    return (datetime.now(timezone.utc) + timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M")


def rows():
    with db.connect() as conn:
        return [dict(r) for r in conn.execute("SELECT kind, caller, status, vapi_call_id FROM reminders")]


async def confirmed(**args):
    call = ToolCall(id="t1", name="schedule_reminder", args=args)
    first = await tools.run(call, TRUSTED)
    if "error" in first:
        return first
    assert first["result"].startswith("CONFIRMATION REQUIRED")
    call.args["confirmed"] = True
    return await tools.run(call, TRUSTED)


async def test_callback_is_dry_run_by_default():
    r = await confirmed(kind="call", when=soon(), message="take the bins out")
    assert r["result"].startswith("Reminder 1 set: I'll call you back on ")
    assert r["result"].endswith("(dry-run: recorded but nothing will actually be sent).")
    assert rows() == [{"kind": "call", "caller": "+14155550100", "status": "dry-run", "vapi_call_id": None}]


async def test_callback_uses_vapi_schedule_plan_when_configured(monkeypatch):
    for k, v in {"DRY_RUN": "false", "VAPI_API_KEY": "sk-test", "VAPI_PHONE_NUMBER_ID": "pn-1"}.items():
        monkeypatch.setenv(k, v)
    get_settings.cache_clear()
    sent = []

    def handler(req):
        sent.append((str(req.url), req.headers["authorization"], json.loads(req.content)))
        return httpx.Response(201, json={"id": "vapi-call-9", "status": "scheduled"})
    real = httpx.AsyncClient
    monkeypatch.setattr(reminders.httpx, "AsyncClient",
                        functools.partial(real, transport=httpx.MockTransport(handler)))
    when = soon(3)
    r = await confirmed(kind="call", when=f"{when}+00:00", message="call the dentist")
    assert "dry-run" not in r["result"]
    url, auth, body = sent[0]
    assert url == "https://api.vapi.ai/call" and auth == "Bearer sk-test"
    assert body["phoneNumberId"] == "pn-1" and body["customer"] == {"number": "+14155550100"}
    assert body["schedulePlan"]["earliestAt"] == f"{when}:00Z"
    assert body["assistant"]["firstMessage"].endswith("call the dentist")
    assert rows()[0]["status"] == "scheduled" and rows()[0]["vapi_call_id"] == "vapi-call-9"


async def test_sms_reminder_is_pending_until_due():
    r = await confirmed(kind="sms", when=soon(), message="stretch")
    assert "text you" in r["result"] and r["result"].endswith("nothing will actually be sent).")
    assert rows()[0]["status"] == "pending"


async def test_bad_times_and_untrusted_callers_are_refused():
    assert "already passed" in (await confirmed(kind="sms", when="2020-01-01T10:00", message="x"))["error"]
    assert "30 days" in (await confirmed(kind="sms", when=soon(24 * 40), message="x"))["error"]
    assert "ISO date-time" in (await confirmed(kind="sms", when="tomorrow", message="x"))["error"]
    stranger = tools.Ctx(call_id="c", caller="+19995550199", trusted=False)
    r = await tools.run(ToolCall(id="t", name="schedule_reminder",
                                 args={"kind": "sms", "when": soon(), "message": "x"}), stranger)
    assert r["error"] == "That action isn't available for this caller."
    assert rows() == []


async def test_dispatcher_sends_due_sms_once_via_outbox():
    now = datetime.now(timezone.utc)
    with db.connect() as conn:
        for due, msg in ((now - timedelta(minutes=1), "due"), (now + timedelta(hours=1), "later")):
            conn.execute("INSERT INTO reminders (caller, kind, due_at, message, status) "
                         "VALUES ('+14155550100', 'sms', ?, ?, 'pending')",
                         (due.isoformat(timespec="seconds"), msg))
    assert await reminders.dispatch_due() == 1
    assert await reminders.dispatch_due() == 0
    with db.connect() as conn:
        assert [tuple(r) for r in conn.execute("SELECT message, status FROM reminders ORDER BY id")] == \
            [("due", "dry-run"), ("later", "pending")]
        assert tuple(conn.execute("SELECT recipient, body, status FROM outbox").fetchone()) == \
            ("+14155550100", "Reminder: due", "dry-run")


def test_callback_payload_leaves_voicemail():
    body = reminders.callback_payload(get_settings(), "+14155550100",
                                      datetime.now(timezone.utc) + timedelta(hours=1), "call the dentist")
    a = body["assistant"]
    assert a["voicemailDetection"] == {"provider": "vapi", "type": "audio"}
    assert a["voicemailMessage"].startswith("Hi, it's your assistant with the reminder you asked for: "
                                            "call the dentist.")


def _scheduled(vapi_call_id="vapi-call-9"):
    with db.connect() as conn:
        conn.execute("INSERT INTO reminders (call_id, caller, kind, due_at, message, status, vapi_call_id) "
                     "VALUES ('call-1', '+14155550100', 'call', '2026-09-26T22:00:00+00:00', "
                     "'call the dentist', 'scheduled', ?)", (vapi_call_id,))


def test_voicemail_report_texts_the_reminder_once():
    from fastapi.testclient import TestClient
    from app.main import app
    from tests.conftest import load_fixture
    _scheduled()
    report = load_fixture("end_of_call_report_voicemail.json")
    with TestClient(app) as c:
        for _ in range(2):  # Vapi may redeliver a webhook
            assert c.post("/vapi/webhook", json=report, headers={"X-Vapi-Secret": "test-secret"}).json() == {"ok": True}
    assert rows()[0]["status"] == "voicemail"
    with db.connect() as conn:
        sms = [dict(r) for r in conn.execute("SELECT channel, recipient, body, status FROM outbox")]
    assert sms == [{"channel": "sms", "recipient": "+14155550100", "status": "dry-run",
                    "body": "Reminder (also left on your voicemail): call the dentist"}]


async def test_answered_callback_is_marked_completed():
    _scheduled()
    assert await reminders.on_callback_ended("vapi-call-9", "customer-ended-call") == "completed"
    assert await reminders.on_callback_ended("vapi-call-9", "voicemail") is None
    assert await reminders.on_callback_ended("unknown", "voicemail") is None

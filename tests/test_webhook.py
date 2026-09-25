import json

import pytest
from fastapi.testclient import TestClient

from app import db, jobs, web_tools
from app.main import app
from app.security import limiter
from tests.conftest import load_fixture

AUTH = {"X-Vapi-Secret": "test-secret"}


@pytest.fixture
def client():
    limiter._hits.clear()
    with TestClient(app) as c:
        yield c


def post(client, fixture, headers=AUTH):
    return client.post("/vapi/webhook", content=json.dumps(load_fixture(fixture)), headers=headers)


def test_rejects_missing_or_wrong_secret(client):
    assert post(client, "status_update_ended.json", headers={}).status_code == 401
    assert post(client, "status_update_ended.json", headers={"X-Vapi-Secret": "x"}).status_code == 401
    r = client.post("/vapi/webhook", content="{not json", headers=AUTH)
    assert r.status_code == 400


def test_assistant_request_returns_transient_assistant(client):
    r = post(client, "assistant_request.json")
    assert r.status_code == 200
    a = r.json()["assistant"]
    assert a["firstMessage"].startswith("Hey")  # +14155550100 is allowlisted
    assert "deep_task" in {t["function"]["name"] for t in a["model"]["tools"]}
    assert db.get_call("call-0001")["caller"] == "+14155550100"


def test_tool_calls_weather_documented_response_shape(client, monkeypatch):
    async def fake_weather(location):
        return f"{location}: sunny, 20°C."
    monkeypatch.setattr(web_tools, "get_weather", fake_weather)
    r = post(client, "tool_calls_weather.json")
    assert r.status_code == 200
    assert r.json() == {"results": [{
        "name": "get_weather",
        "toolCallId": "toolu_01DTPAzUm5Gk3zxrpJ969oMF",
        "result": "San Francisco: sunny, 20°C.",
    }]}


def test_tool_calls_side_effect_needs_confirmation(client):
    r = post(client, "tool_calls_followup.json").json()["results"][0]
    assert r["toolCallId"] == "call_VaJOd8ZeZgWCEHDYomyCPfwN"
    assert r["result"].startswith("CONFIRMATION REQUIRED")


def test_untrusted_caller_cannot_use_agentic_tools(client):
    payload = load_fixture("tool_calls_followup.json")
    payload["message"]["call"]["customer"]["number"] = "+19995550199"
    r = client.post("/vapi/webhook", json=payload, headers=AUTH).json()
    assert r["results"][0]["error"] == "That action isn't available for this caller."


def test_end_of_call_report_stores_transcript_and_summary(client):
    assert post(client, "end_of_call_report.json").json() == {"ok": True}
    row = db.get_call("call-0001")
    assert row["status"] == "ended" and row["ended_reason"] == "hangup"
    assert row["transcript"].startswith("AI: How can I help?")
    assert row["summary"].startswith("The caller asked about the weather")


def test_status_update_ended_releases_finished_jobs(client, monkeypatch):
    async def fake(*a, **kw):
        return "report"
    monkeypatch.setattr(jobs.llm, "complete", fake)
    db.upsert_call("call-0001", status="in-progress")
    with db.connect() as conn:
        conn.execute("INSERT INTO jobs (call_id, task, channel, recipient, status, result) "
                     "VALUES ('call-0001', 't', 'sms', '+14155550100', 'done', 'report')")
    post(client, "status_update_ended.json")
    with db.connect() as conn:
        assert conn.execute("SELECT delivered FROM jobs").fetchone()[0] == 1
        assert conn.execute("SELECT status FROM outbox").fetchone()[0] == "dry-run"


def test_unhandled_message_types_are_acknowledged(client):
    r = client.post("/vapi/webhook", json={"message": {"type": "speech-update"}}, headers=AUTH)
    assert r.json() == {"ok": True}


def test_end_of_call_report_without_summary_uses_claude(client, monkeypatch):
    async def fake(prompt, system, **kw):
        assert "What's the weather" in prompt
        return "Caller checked the weather."
    monkeypatch.setattr(jobs.llm, "complete", fake)
    payload = load_fixture("end_of_call_report.json")
    del payload["message"]["analysis"]
    client.post("/vapi/webhook", json=payload, headers=AUTH)
    assert db.get_call("call-0001")["summary"] == "Caller checked the weather."


def test_web_page_and_public_config(client, monkeypatch):
    monkeypatch.setenv("VAPI_PUBLIC_KEY", "pub_123")
    monkeypatch.setenv("VAPI_API_KEY", "private-never-exposed")
    assert "@vapi-ai/web" in client.get("/").text
    cfg = client.get("/web/config").json()
    assert cfg == {"publicKey": "pub_123", "assistantId": ""}


def test_request_id_is_generated_or_propagated(client):
    r = client.get("/healthz")
    assert len(r.headers["x-request-id"]) == 16
    assert client.get("/healthz", headers={"X-Request-ID": "abc-123"}).headers["x-request-id"] == "abc-123"
    # Junk IDs (log injection, huge values) are replaced rather than echoed.
    assert client.get("/healthz", headers={"X-Request-ID": "a b\nc"}).headers["x-request-id"] != "a b\nc"


def test_log_lines_carry_request_id():
    import logging
    from app.logs import JsonFormatter, request_id
    token = request_id.set("rid-1")
    try:
        rec = logging.LogRecord("voice_agent", logging.INFO, "", 0, "x", None, None)
        assert json.loads(JsonFormatter().format(rec))["request_id"] == "rid-1"
    finally:
        request_id.reset(token)


def test_end_of_call_report_updates_memory_for_trusted_callers_only(client, monkeypatch):
    from app import memory

    async def no_claude(*a, **kw):
        return None
    monkeypatch.setattr(memory.llm, "complete", no_claude)
    payload = load_fixture("end_of_call_report.json")
    payload["message"]["artifact"]["messages"].append(
        {"role": "user", "message": "By the way, my name is Priya."})
    client.post("/vapi/webhook", json=payload, headers=AUTH)
    assert memory.facts_for("+14155550100") == ["Their name is Priya."]
    payload["message"]["call"]["customer"]["number"] = "+19995550199"
    client.post("/vapi/webhook", json=payload, headers=AUTH)
    assert memory.facts_for("+19995550199") == []


def test_assistant_request_includes_caller_memory(client):
    from app import memory
    memory.save("+14155550100", ["Their name is Priya."], "old-call", "rules")
    prompt = post(client, "assistant_request.json").json()["assistant"]["model"]["messages"][0]["content"]
    assert "- Their name is Priya." in prompt


def test_assistant_request_prefetches_remembered_home_weather(client, monkeypatch):
    from app import memory
    warmed = []
    monkeypatch.setattr(web_tools, "prefetch_weather", warmed.append)
    memory.save("+14155550100", ["They live in Oakland."], "old-call", "rules")
    post(client, "assistant_request.json")
    assert warmed == ["Oakland"]


def test_transfer_destination_only_after_confirmed_request(client, monkeypatch):
    from app.config import get_settings
    from app import tools
    monkeypatch.setenv("TRANSFER_NUMBER", "+14155550123")
    get_settings.cache_clear()
    assert "error" in post(client, "transfer_destination_request.json").json()
    ask = {"message": {"type": "tool-calls", "call": {"id": "call-0001", "type": "inboundPhoneCall",
           "customer": {"number": "+14155550100"}},
           "toolCallList": [{"id": "t1", "name": "request_transfer", "parameters": {}}]}}
    client.post("/vapi/webhook", json=ask, headers=AUTH)
    assert "error" in post(client, "transfer_destination_request.json").json()  # not yet confirmed
    ask["message"]["toolCallList"][0]["parameters"] = {"confirmed": True}
    r = client.post("/vapi/webhook", json=ask, headers=AUTH).json()["results"][0]
    assert r["result"].startswith("Transfer approved")
    assert post(client, "transfer_destination_request.json").json() == {"destination": {
        "type": "number", "number": "+14155550123", "message": "Connecting you now."}}
    assert "error" in post(client, "transfer_destination_request.json").json()  # single use
    assert tools.transfer_approved("call-0001") is False


def test_transfer_refused_for_untrusted_caller_even_if_approved(client, monkeypatch):
    from app.config import get_settings
    from app import db as _db
    monkeypatch.setenv("TRANSFER_NUMBER", "+14155550123")
    get_settings.cache_clear()
    import time
    with _db.connect() as conn:
        conn.execute("INSERT INTO confirmations VALUES ('call-0001', 'transfer.approved', '', ?)", (time.time(),))
    payload = load_fixture("transfer_destination_request.json")
    payload["message"]["customer"]["number"] = payload["message"]["call"]["customer"]["number"] = "+19995550199"
    assert "error" in client.post("/vapi/webhook", json=payload, headers=AUTH).json()


def test_transfer_update_is_acknowledged(client):
    msg = {"message": {"type": "transfer-update", "call": {"id": "call-0001"},
                       "destination": {"type": "number", "number": "+14155550123"}}}
    assert client.post("/vapi/webhook", json=msg, headers=AUTH).json() == {"ok": True}


@pytest.mark.parametrize("fixture,name,fn,expected", [
    ("tool_calls_convert.json", "convert", ("convert", "convert"), "250 USD is about"),
    ("tool_calls_wikipedia.json", "wikipedia", ("web_tools", "wikipedia"), "From Wikipedia"),
    ("tool_calls_news.json", "news_headlines", ("web_tools", "headlines"), "BBC technology"),
])
def test_new_tool_fixtures_round_trip(client, monkeypatch, fixture, name, fn, expected):
    from app import convert as convert_mod

    async def fake(*args):
        return {"convert": "250 USD is about 219.94 EUR.", "wikipedia": "From Wikipedia, Ada.",
                "headlines": "BBC technology headlines: 1. X."}[fn[1]]
    monkeypatch.setattr({"convert": convert_mod, "web_tools": web_tools}[fn[0]], fn[1], fake)
    body = post(client, fixture).json()
    tc = load_fixture(fixture)["message"]["toolCallList"][0]
    assert body["results"][0]["name"] == name and body["results"][0]["toolCallId"] == tc["id"]
    assert body["results"][0]["result"].startswith(expected)

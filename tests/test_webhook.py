import json

import pytest
from fastapi.testclient import TestClient

from app import db, web_tools
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


def test_unhandled_message_types_are_acknowledged(client):
    r = client.post("/vapi/webhook", json={"message": {"type": "speech-update"}}, headers=AUTH)
    assert r.json() == {"ok": True}

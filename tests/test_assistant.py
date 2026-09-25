from dataclasses import replace

from app.assistant import build_assistant
from app.config import get_settings


def names(a):
    return {t["function"]["name"] for t in a["model"]["tools"]}


def test_trusted_caller_gets_all_tools():
    a = build_assistant(get_settings(), trusted=True)
    assert names(a) >= {"send_followup", "deep_task", "create_event", "get_weather"}
    tool = next(t for t in a["model"]["tools"] if t["function"]["name"] == "send_followup")
    assert tool["type"] == "function"
    assert "confirmed" in tool["function"]["parameters"]["properties"]
    assert tool["server"] == {"url": "https://agent.example.com/vapi/webhook", "timeoutSeconds": 20,
                              "headers": {"X-Vapi-Secret": "test-secret"}}
    assert tool["messages"][0]["type"] == "request-start"
    assert a["serverMessages"] == ["tool-calls", "status-update", "end-of-call-report"]


def test_untrusted_caller_gets_read_only_tools():
    a = build_assistant(get_settings(), trusted=False)
    assert names(a) == {"get_weather", "web_search", "fetch_url"}
    assert "not on the allowlist" in a["model"]["messages"][0]["content"]


def test_prompt_uses_vapi_liquid_date_and_credential_id():
    s = replace(get_settings(), vapi_credential_id="cred_1", timezone="Europe/London")
    a = build_assistant(s, trusted=True)
    prompt = a["model"]["messages"][0]["content"]
    assert '{{"now" | date: "%A, %B %d, %Y, %H:%M", "Europe/London"}}' in prompt
    assert a["server"] == {"url": "https://agent.example.com/vapi/webhook", "timeoutSeconds": 20,
                           "credentialId": "cred_1"}


def test_owner_name_wording():
    s = get_settings()
    a = build_assistant(replace(s, owner_name=""), trusted=True)
    assert a["firstMessage"] == "Hey, what can I do for you?"
    assert a["model"]["messages"][0]["content"].startswith("You are a personal voice assistant")
    a = build_assistant(replace(s, owner_name="Sam"), trusted=True)
    assert a["firstMessage"] == "Hey Sam, what can I do for you?"
    assert a["model"]["messages"][0]["content"].startswith("You are Sam's personal voice assistant")

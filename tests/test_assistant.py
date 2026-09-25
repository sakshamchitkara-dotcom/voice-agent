from dataclasses import replace

from app.assistant import build_assistant
from app.config import get_settings
from app.tools import TOOLS


def names(a):
    return {t["function"]["name"] if "function" in t else t["type"] for t in a["model"]["tools"]}


def test_trusted_caller_gets_all_tools():
    a = build_assistant(get_settings(), trusted=True)
    assert names(a) >= {"send_followup", "deep_task", "create_event", "get_weather"}
    tool = next(t for t in a["model"]["tools"] if t["function"]["name"] == "send_followup")
    assert tool["type"] == "function"
    assert "confirmed" in tool["function"]["parameters"]["properties"]
    assert tool["server"] == {"url": "https://agent.example.com/vapi/webhook", "timeoutSeconds": 20,
                              "headers": {"X-Vapi-Secret": "test-secret"}}
    assert [m["type"] for m in tool["messages"]] == ["request-start", "request-response-delayed"]
    assert tool["messages"][1]["timingMilliseconds"] == 1000
    assert a["serverMessages"] == ["tool-calls", "status-update", "end-of-call-report",
                                   "transfer-destination-request", "transfer-update"]


def test_untrusted_caller_gets_read_only_tools():
    a = build_assistant(get_settings(), trusted=False)
    assert names(a) == {name for name, t in TOOLS.items() if not t.agentic and t.enabled(get_settings())}
    assert {"get_weather", "web_search", "fetch_url", "convert"} <= names(a)
    assert not names(a) & {"add_note", "send_followup", "deep_task"}
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


def test_memories_are_injected_for_trusted_callers_only():
    facts = ["Their name is Priya.", "They prefer texts."]
    prompt = build_assistant(get_settings(), trusted=True, memories=facts)["model"]["messages"][0]["content"]
    assert prompt.endswith("<memory>\n- Their name is Priya.\n- They prefer texts.\n</memory>")
    untrusted = build_assistant(get_settings(), trusted=False, memories=facts)
    assert "<memory>" not in untrusted["model"]["messages"][0]["content"]
    assert "<memory>" not in build_assistant(get_settings(), trusted=True)["model"]["messages"][0]["content"]


def test_request_transfer_only_offered_when_configured():
    s = get_settings()
    assert "request_transfer" not in names(build_assistant(s, trusted=True))
    assert "request_transfer" in names(build_assistant(replace(s, transfer_number="+14155550123"), trusted=True))
    assert "request_transfer" not in names(build_assistant(replace(s, transfer_number="+14155550123"), trusted=False))


def test_transfer_call_tool_has_no_static_destination():
    s = replace(get_settings(), transfer_number="+14155550123", owner_name="Sam")
    a = build_assistant(s, trusted=True)
    assert {"type": "transferCall", "destinations": []} in a["model"]["tools"]
    assert "talk to Sam directly, call request_transfer" in a["model"]["messages"][0]["content"]
    assert not [t for t in build_assistant(s, trusted=False)["model"]["tools"] if t["type"] == "transferCall"]
    assert not [t for t in build_assistant(get_settings(), trusted=True)["model"]["tools"]
                if t["type"] == "transferCall"]


def test_caller_language_by_longest_prefix(monkeypatch):
    from app.assistant import caller_language
    monkeypatch.setenv("CALLER_LANGUAGES", "+34=es, +1=en, +1514=fr")
    get_settings.cache_clear()
    s = get_settings()
    assert caller_language("+34911222333", s) == "es"
    assert caller_language("+15145550100", s) == "fr"  # Montreal beats +1
    assert caller_language("+14155550100", s) == "en" and caller_language(None, s) == "en"


def test_spanish_caller_gets_spanish_greeting_transcriber_and_voice():
    s = replace(get_settings(), owner_name="Sam")
    a = build_assistant(s, trusted=True, language="es")
    assert a["firstMessage"] == "Hola Sam, ¿en qué te puedo ayudar?"
    assert a["transcriber"] == {"provider": "deepgram", "model": "nova-3", "language": "es"}
    assert a["voice"] == {"provider": "azure", "voiceId": "multilingual-auto"}
    assert "Speak Spanish on this call" in a["model"]["messages"][0]["content"]
    assert a["metadata"]["language"] == "es"
    english = build_assistant(s, trusted=True)
    assert "transcriber" not in english and english["firstMessage"] == "Hey Sam, what can I do for you?"


def test_bad_caller_languages_setting_is_rejected(monkeypatch):
    import pytest
    from app.config import load_settings
    monkeypatch.setenv("CALLER_LANGUAGES", "34=klingon")
    with pytest.raises(ValueError, match="CALLER_LANGUAGES entries look like"):
        load_settings()

import pytest

from scripts import vapi_setup


@pytest.fixture
def calls(monkeypatch):
    seen = []

    class R:
        is_error = False

        def __init__(self, body):
            self.body = body

        def json(self):
            return self.body

    def fake_request(method, url, json=None, **kw):
        seen.append((method, url, json, kw["headers"]["Authorization"]))
        return R({"id": "asst_new", "number": "+14155559999"})
    monkeypatch.setenv("VAPI_API_KEY", "k")
    monkeypatch.setattr(vapi_setup.httpx, "request", fake_request)
    return seen


def test_creates_then_updates_assistant(calls, monkeypatch, capsys):
    vapi_setup.main(["assistant"])
    assert calls[0][:2] == ("POST", "https://api.vapi.ai/assistant")
    assert calls[0][3] == "Bearer k"
    monkeypatch.setenv("VAPI_ASSISTANT_ID", "asst_new")
    vapi_setup.main(["assistant"])
    assert calls[1][:2] == ("PATCH", "https://api.vapi.ai/assistant/asst_new")
    assert "Updated assistant asst_new" in capsys.readouterr().out


def test_phone_dynamic_points_number_at_webhook(calls):
    vapi_setup.main(["phone", "--number-id", "pn_1"])
    method, url, body, _ = calls[0]
    assert (method, url) == ("PATCH", "https://api.vapi.ai/phone-number/pn_1")
    assert body["assistantId"] is None
    assert body["server"]["url"] == "https://agent.example.com/vapi/webhook"


def test_requires_api_key(monkeypatch):
    monkeypatch.delenv("VAPI_API_KEY", raising=False)
    with pytest.raises(SystemExit, match="VAPI_API_KEY"):
        vapi_setup.main(["phone", "--list"])

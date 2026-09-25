from types import SimpleNamespace

import anthropic
import httpx2 as httpx

from app import llm


def _resp(text, stop="end_turn"):
    return SimpleNamespace(
        stop_reason=stop,
        content=[SimpleNamespace(type="text", text=text)],
        usage=SimpleNamespace(input_tokens=1, output_tokens=1),
    )


class FakeMessages:
    def __init__(self, behaviour):
        self.behaviour, self.models = behaviour, []

    async def create(self, **kw):
        self.models.append(kw["model"])
        b = self.behaviour[kw["model"]]
        if isinstance(b, Exception):
            raise b
        return b


def _client(monkeypatch, behaviour):
    fake = SimpleNamespace(messages=FakeMessages(behaviour))
    monkeypatch.setattr(llm, "_get_client", lambda: fake)
    return fake.messages


def _status_error(cls, code):
    req = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    return cls("x", response=httpx.Response(code, request=req), body=None)


async def test_primary_model_used(monkeypatch):
    m = _client(monkeypatch, {"claude-opus-5-5": _resp("hi")})
    assert await llm.complete("p", "s") == "hi"
    assert m.models == ["claude-opus-5-5"]


async def test_falls_back_on_overload_and_refusal(monkeypatch):
    m = _client(monkeypatch, {
        "claude-opus-5-5": _status_error(anthropic.InternalServerError, 529),
        "claude-opus-5": _resp("from fallback"),
    })
    assert await llm.complete("p", "s") == "from fallback"
    m = _client(monkeypatch, {"claude-opus-5-5": _resp("", "refusal"), "claude-opus-5": _resp("ok")})
    assert await llm.complete("p", "s") == "ok"


async def test_returns_none_without_credentials(monkeypatch):
    _client(monkeypatch, {"claude-opus-5-5": TypeError("Could not resolve authentication method")})
    assert await llm.complete("p", "s") is None


async def test_pause_turn_is_resumed(monkeypatch):
    calls = iter([_resp("", "pause_turn"), _resp("final")])

    class M:
        async def create(self, **kw):
            return next(calls)
    monkeypatch.setattr(llm, "_get_client", lambda: SimpleNamespace(messages=M()))
    assert await llm.complete("p", "s", web_search=True) == "final"

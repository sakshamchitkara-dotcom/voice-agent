import hashlib
import hmac
from dataclasses import replace

from starlette.datastructures import Headers

from app.config import get_settings
from app.security import RateLimiter, is_trusted, verify_webhook

BODY = b'{"message":{"type":"status-update"}}'


def h(**kv):
    return Headers({k.replace("_", "-"): v for k, v in kv.items()})


def test_shared_secret_header_and_bearer():
    s = get_settings()
    assert verify_webhook(h(x_vapi_secret="test-secret"), BODY, s)
    assert verify_webhook(h(authorization="Bearer test-secret"), BODY, s)
    assert not verify_webhook(h(x_vapi_secret="wrong"), BODY, s)
    assert not verify_webhook(h(), BODY, s)


def test_hmac_signature():
    s = replace(get_settings(), webhook_secret="", hmac_secret="k")
    sig = hmac.new(b"k", BODY, hashlib.sha256).hexdigest()
    assert verify_webhook(h(x_vapi_signature=sig), BODY, s)
    assert verify_webhook(h(x_vapi_signature=f"sha256={sig}"), BODY, s)
    assert not verify_webhook(h(x_vapi_signature=sig), BODY + b" ", s)


def test_no_secret_configured_rejects_unless_opted_out():
    s = replace(get_settings(), webhook_secret="", hmac_secret="")
    assert not verify_webhook(h(), BODY, s)
    assert verify_webhook(h(), BODY, replace(s, allow_unauthenticated=True))


def test_allowlist():
    s = get_settings()
    assert is_trusted("+14155550100", "inboundPhoneCall", s)
    assert not is_trusted("+19999999999", "inboundPhoneCall", s)
    assert not is_trusted(None, "inboundPhoneCall", s)
    assert not is_trusted(None, "webCall", s)
    assert is_trusted(None, "webCall", replace(s, allow_web_agentic=True))


def test_rate_limiter_window():
    rl = RateLimiter()
    assert all(rl.allow("a", 2, 60) for _ in range(2))
    assert not rl.allow("a", 2, 60)
    assert rl.allow("b", 2, 60)

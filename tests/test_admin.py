import pytest
from fastapi.testclient import TestClient

from app import db, memory, tools
from app.config import get_settings
from app.main import app
from app.security import limiter
from app.vapi import ToolCall

ADMIN = ("admin", "s3cret-pass")


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("ADMIN_PASSWORD", ADMIN[1])
    get_settings.cache_clear()
    limiter._hits.clear()
    with TestClient(app) as c:
        yield c


def test_admin_is_off_without_a_password(monkeypatch):
    monkeypatch.delenv("ADMIN_PASSWORD", raising=False)
    get_settings.cache_clear()
    with TestClient(app) as c:
        assert c.get("/admin", auth=ADMIN).status_code == 404


def test_admin_requires_basic_auth(client):
    r = client.get("/admin")
    assert r.status_code == 401 and r.headers["www-authenticate"].startswith("Basic")
    assert client.get("/admin", auth=("admin", "wrong")).status_code == 401
    assert client.get("/admin", auth=("root", ADMIN[1])).status_code == 401
    ok = client.get("/admin", auth=ADMIN)
    assert ok.status_code == 200 and "default-src 'none'" in ok.headers["content-security-policy"]


async def test_call_detail_shows_transcript_tool_calls_and_memory(client):
    db.upsert_call("call-9", caller="+14155550100", status="ended", summary="Asked about <b>rain</b>",
                   transcript="AI: Hi <script>x</script> User: note please")
    ctx = tools.Ctx(call_id="call-9", caller="+14155550100", trusted=True)
    await tools.run(ToolCall(id="t1", name="add_note", args={"text": "milk"}), ctx)
    memory.save("+14155550100", ["Their name is Priya."], "call-9", "rules")

    listing = client.get("/admin", auth=ADMIN).text
    assert "<a href='/admin/calls/call-9'>call-9</a>" in listing
    assert "Asked about &lt;b&gt;rain&lt;/b&gt;" in listing  # escaped
    detail = client.get("/admin/calls/call-9", auth=ADMIN).text
    assert "&lt;script&gt;" in detail and "<script>" not in detail
    assert "add_note" in detail and "Saved note 1." in detail and "Their name is Priya." in detail
    assert client.get("/admin/calls/nope", auth=ADMIN).status_code == 404

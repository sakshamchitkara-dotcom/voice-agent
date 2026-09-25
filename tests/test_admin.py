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


def test_outbox_jobs_and_reminders_pages(client):
    with db.connect() as conn:
        conn.execute("INSERT INTO outbox (channel, recipient, subject, body, status) "
                     "VALUES ('sms', '+14155550100', 'Hi', 'See you Friday', 'dry-run')")
        conn.execute("INSERT INTO jobs (call_id, task, channel, recipient, status) "
                     "VALUES ('call-1', 'Compare e-bikes', 'email', 'me@x.io', 'running')")
        conn.execute("INSERT INTO reminders (call_id, caller, kind, due_at, message, status) "
                     "VALUES ('call-1', '+14155550100', 'call', '2030-01-01T10:00:00+00:00', 'bins', 'dry-run')")
    assert "See you Friday" in client.get("/admin/outbox", auth=ADMIN).text
    jobs = client.get("/admin/jobs", auth=ADMIN).text
    assert "Compare e-bikes" in jobs and "/admin/calls/call-1" in jobs
    assert "bins" in client.get("/admin/reminders", auth=ADMIN).text
    assert client.get("/admin/outbox").status_code == 401


def test_calendar_ics_export(client, monkeypatch):
    from datetime import datetime, timezone
    from app.admin import to_ics
    monkeypatch.setenv("TIMEZONE", "America/Los_Angeles")
    get_settings.cache_clear()
    with db.connect() as conn:
        conn.execute("INSERT INTO events (caller, title, starts_at, notes) VALUES "
                     "('+14155550100', 'Dentist; bring forms, card', '2030-01-03T09:00', 'Line 1\nLine 2')")
        conn.execute("INSERT INTO events (caller, title, starts_at) VALUES ('+19995550199', 'Other', '2030-07-01T10:00')")
    assert client.get("/admin/calendar.ics").status_code == 401
    r = client.get("/admin/calendar.ics", params={"caller": "+14155550100"}, auth=ADMIN)
    assert r.headers["content-type"] == "text/calendar; charset=utf-8"
    assert "DTSTART:20300103T170000Z\r\n" in r.text  # 09:00 PST = 17:00 UTC
    assert "SUMMARY:Dentist\; bring forms\\, card\r\n" in r.text and "DESCRIPTION:Line 1\\nLine 2" in r.text
    assert "Other" not in r.text and r.text.startswith("BEGIN:VCALENDAR\r\nVERSION:2.0\r\n")
    everything = to_ics([{"id": 1, "title": "x" * 200, "starts_at": "2030-07-01T10:00", "notes": None}],
                        "America/Los_Angeles", now=datetime(2030, 1, 1, tzinfo=timezone.utc))
    assert "DTSTART:20300701T170000Z" in everything  # PDT in July
    assert all(len(line.encode()) <= 75 for line in everything.split("\r\n"))
    assert "\r\n x" in everything  # folded continuation line


def test_analytics_page(client):
    from app.admin import analytics_data, pct
    db.upsert_call("a", caller="+1", started_at="2099-01-01T10:00:00Z", ended_reason="customer-ended-call")
    db.upsert_call("b", caller="+1", started_at="2099-01-01T11:00:00Z", ended_reason="voicemail")
    with db.connect() as conn:
        for outcome, ms in [("ok", 100), ("ok", 300), ("error", 900), ("deferred", 1500)]:
            conn.execute("INSERT INTO tool_calls (call_id, tool, outcome, ms) VALUES ('a', 'get_weather', ?, ?)",
                         (outcome, ms))
    [weather] = analytics_data()["tools"]
    assert (weather["calls"], weather["ok"], weather["error"], weather["deferred"]) == (4, 2, 1, 1)
    assert weather["error_rate"] == "25%" and (weather["p50_ms"], weather["p95_ms"]) == (300, 1500)
    assert pct([], 50) is None and pct([5], 95) == 5
    r = client.get("/admin/analytics", auth=ADMIN)
    assert r.status_code == 200 and "Calls per day" in r.text and "2099-01-01" in r.text
    assert "<td>voicemail</td><td>1</td>" in r.text and "class=bar" in r.text

import asyncio

from app import db, jobs, llm


async def _fake_llm(*a, **kw):
    return "Answer: 42. Source: https://example.com"


def outbox():
    with db.connect() as conn:
        return [dict(r) for r in conn.execute("SELECT channel, recipient, body FROM outbox")]


async def test_job_waits_for_call_end_then_delivers(monkeypatch):
    monkeypatch.setattr(llm, "complete", _fake_llm)
    db.upsert_call("call-1", status="in-progress")
    job_id = jobs.enqueue("call-1", "+14155550100", "meaning of life", "sms", "+14155550100")
    await asyncio.gather(*jobs._running)
    assert jobs.get_job(job_id)["status"] == "done"
    assert outbox() == []  # call still live

    db.upsert_call("call-1", status="ended")
    await jobs.deliver_for_call("call-1")
    await jobs.deliver_for_call("call-1")  # idempotent
    assert outbox() == [{"channel": "sms", "recipient": "+14155550100",
                         "body": "Answer: 42. Source: https://example.com"}]


async def test_job_falls_back_to_search_without_claude(monkeypatch):
    async def none(*a, **kw):
        return None

    async def fake_search(q, limit=5):
        return [{"title": "T", "url": "https://t.example", "snippet": "S"}]
    monkeypatch.setattr(llm, "complete", none)
    monkeypatch.setattr(jobs.web_tools, "search", fake_search)
    job_id = jobs.enqueue(None, None, "x", "email", "a@b.co")
    await asyncio.gather(*jobs._running)
    job = jobs.get_job(job_id)
    assert job["status"] == "done" and job["delivered"] == 1
    assert job["result"].startswith("Claude was unavailable") and "https://t.example" in job["result"]

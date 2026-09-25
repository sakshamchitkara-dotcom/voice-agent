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


async def test_two_workers_resuming_run_each_job_once(monkeypatch):
    runs = []

    async def counting(*a, **kw):
        runs.append(a[0])
        return "done"
    monkeypatch.setattr(llm, "complete", counting)
    with db.connect() as conn:
        conn.execute("INSERT INTO jobs (id, task, channel, recipient, status) "
                     "VALUES (1, 'interrupted', 'email', 'a@b.co', 'running')")
    jobs.resume_pending()
    jobs.resume_pending()  # a second worker starting at the same time
    await asyncio.gather(*jobs._running)
    assert runs == ["interrupted"] and jobs.get_job(1)["status"] == "done"


async def test_shared_mode_leaves_fresh_running_jobs_to_their_worker(monkeypatch):
    from app.config import get_settings
    monkeypatch.setenv("SHARED_STATE", "sqlite")
    get_settings.cache_clear()
    with db.connect() as conn:
        conn.execute("INSERT INTO jobs (id, task, channel, recipient, status) "
                     "VALUES (1, 'busy elsewhere', 'email', 'a@b.co', 'running')")
    jobs.resume_pending()
    assert not jobs._running and jobs.get_job(1)["status"] == "running"

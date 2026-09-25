from app import db


def test_upsert_call_merges_fields():
    db.upsert_call("c1", caller="+1", status="in-progress")
    db.upsert_call("c1", status="ended", summary=None)
    row = db.get_call("c1")
    assert row["caller"] == "+1"
    assert row["status"] == "ended"
    assert row["summary"] is None


def _init_after(path, barrier, results):
    import os
    os.environ["DB_PATH"] = path
    from app.config import get_settings
    get_settings.cache_clear()
    barrier.wait()
    try:
        db.init_db()
        results.put("ok")
    except Exception as e:
        results.put(repr(e))


def test_workers_starting_together_on_a_fresh_file_all_start(tmp_path):
    import multiprocessing as mp
    ctx = mp.get_context("spawn")
    for trial in range(3):  # without the retry, 8 at once hit "database is locked" most runs
        path = str(tmp_path / f"fresh{trial}.db")
        barrier, results = ctx.Barrier(8), ctx.Queue()
        procs = [ctx.Process(target=_init_after, args=(path, barrier, results)) for _ in range(8)]
        for p in procs:
            p.start()
        for p in procs:
            p.join(30)
        assert [results.get(timeout=5) for _ in procs] == ["ok"] * 8

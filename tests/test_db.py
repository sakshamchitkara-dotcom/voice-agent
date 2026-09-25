from app import db


def test_upsert_call_merges_fields():
    db.upsert_call("c1", caller="+1", status="in-progress")
    db.upsert_call("c1", status="ended", summary=None)
    row = db.get_call("c1")
    assert row["caller"] == "+1"
    assert row["status"] == "ended"
    assert row["summary"] is None

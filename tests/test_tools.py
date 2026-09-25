import asyncio

import pytest

from app import db, tools
from app.config import get_settings
from app.security import limiter
from app.vapi import ToolCall

TRUSTED = tools.Ctx(call_id="call-1", caller="+14155550100", trusted=True)
STRANGER = tools.Ctx(call_id="call-2", caller="+19995550199", trusted=False)


@pytest.fixture(autouse=True)
def fresh_limiter():
    limiter._hits.clear()


def tc(name, **args):
    return ToolCall(id=f"id-{name}", name=name, args=args)


def outbox():
    with db.connect() as conn:
        return [dict(r) for r in conn.execute("SELECT channel, recipient, body, status FROM outbox")]


async def test_unknown_tool_and_missing_args():
    assert "Unknown tool" in (await tools.run(tc("nope"), TRUSTED))["error"]
    r = await tools.run(tc("get_weather"), TRUSTED)
    assert r == {"name": "get_weather", "toolCallId": "id-get_weather",
                 "error": "Missing required argument(s): location."}


async def test_agentic_tools_denied_for_untrusted_callers():
    r = await tools.run(tc("add_note", text="x"), STRANGER)
    assert r["error"] == "That action isn't available for this caller."


async def test_notes_round_trip_per_caller():
    assert (await tools.run(tc("add_note", text="buy milk"), TRUSTED))["result"] == "Saved note 1."
    listed = (await tools.run(tc("list_notes"), TRUSTED))["result"]
    assert "buy milk" in listed
    web = tools.Ctx(call_id="w", caller=None, trusted=True)
    assert (await tools.run(tc("list_notes"), web))["result"] == "You have no notes."


async def test_side_effects_need_a_read_back_confirmation():
    msg = tc("send_followup", channel="sms", message="See you Friday")
    # Jumping straight to confirmed=true without a pending confirmation is not enough.
    first = await tools.run(tc("send_followup", channel="sms", message="See you Friday", confirmed=True), TRUSTED)
    assert first["result"].startswith("CONFIRMATION REQUIRED")
    assert "+14155550100" in first["result"] and outbox() == []

    msg.args["confirmed"] = True
    done = await tools.run(msg, TRUSTED)
    assert done["result"].startswith("Done: dry-run: sms to +14155550100")
    assert outbox() == [{"channel": "sms", "recipient": "+14155550100",
                         "body": "See you Friday", "status": "dry-run"}]
    # The confirmation is consumed: repeating asks again.
    again = await tools.run(msg, TRUSTED)
    assert again["result"].startswith("CONFIRMATION REQUIRED")


async def test_changed_arguments_invalidate_confirmation():
    await tools.run(tc("create_event", title="Dentist", starts_at="2030-01-02T15:00"), TRUSTED)
    r = await tools.run(tc("create_event", title="Dentist", starts_at="2030-01-03T15:00",
                           confirmed=True), TRUSTED)
    assert r["result"].startswith("CONFIRMATION REQUIRED")
    r = await tools.run(tc("create_event", title="Dentist", starts_at="2030-01-03T15:00",
                           confirmed=True), TRUSTED)
    assert r["result"] == "Added Dentist on Thursday January 03 at 15:00."
    assert "Dentist on Thursday January 03" in (await tools.run(tc("list_events"), TRUSTED))["result"]


async def test_bad_event_date_is_a_spoken_error():
    args = dict(title="X", starts_at="next tuesday")
    await tools.run(tc("create_event", **args), TRUSTED)
    r = await tools.run(tc("create_event", **args, confirmed=True), TRUSTED)
    assert "ISO date-time" in r["error"]


async def test_email_needs_an_address():
    r = await tools.run(tc("send_followup", channel="email", message="hi"), TRUSTED)
    assert r["error"] == "I need an email address to send that to."


async def test_rate_limit(monkeypatch):
    monkeypatch.setenv("RATE_LIMIT_PER_MINUTE", "2")
    from app.config import get_settings
    get_settings.cache_clear()
    for _ in range(2):
        await tools.run(tc("list_notes"), TRUSTED)
    r = await tools.run(tc("list_notes"), TRUSTED)
    assert r["error"].startswith("Too many requests")


async def test_slow_tool_times_out(monkeypatch):
    async def slow(a, ctx):
        await asyncio.sleep(1)
    monkeypatch.setattr(tools, "TOOL_TIMEOUT_S", 0.01)
    monkeypatch.setattr(tools.TOOLS["list_notes"], "handler", slow)
    r = await tools.run(tc("list_notes"), TRUSTED)
    assert "took too long" in r["error"]


async def test_slow_lookup_defers_then_retry_hits_warm_cache(monkeypatch):
    from app import metrics, web_tools
    monkeypatch.setenv("TOOL_SOFT_DEADLINE_S", "0.05")
    get_settings.cache_clear()
    upstream = []

    @web_tools.ttl_cache(60)
    async def slow_weather(location):
        upstream.append(location)
        await asyncio.sleep(0.2)
        return f"{location}: sunny."
    monkeypatch.setattr(web_tools, "get_weather", slow_weather)
    metrics.reset()
    first = await tools.run(tc("get_weather", location="Oslo"), TRUSTED)
    assert first["result"].startswith("STILL WORKING") and "call get_weather again" in first["result"]
    await asyncio.sleep(0.3)  # background lookup finishes and fills the cache
    second = await tools.run(tc("get_weather", location="Oslo"), TRUSTED)
    assert second["result"] == "Oslo: sunny." and upstream == ["Oslo"]
    assert 'voice_agent_tool_calls_total{tool="get_weather",outcome="deferred"} 1' in metrics.render()


async def test_deep_task_queues_after_confirmation(monkeypatch):
    queued = []
    monkeypatch.setattr(tools.jobs, "enqueue", lambda *a: queued.append(a) or 7)
    call = tc("deep_task", task="Compare three e-bikes under $2k", channel="email", email="me@x.io")
    assert (await tools.run(call, TRUSTED))["result"].startswith("CONFIRMATION REQUIRED")
    call.args["confirmed"] = True
    r = await tools.run(call, TRUSTED)
    assert r["result"] == "Queued task 7. I'll send the results by email to me@x.io after we hang up."
    assert queued == [("call-1", "+14155550100", "Compare three e-bikes under $2k", "email", "me@x.io")]


async def test_confirmation_prompt_has_no_double_period():
    r = await tools.run(tc("send_followup", channel="sms", message="Moved to 3pm."), TRUSTED)
    assert "3pm.." not in r["result"] and "3pm. If they clearly say yes" in r["result"]


async def test_tool_metrics_record_outcomes():
    from app import metrics
    metrics.reset()
    await tools.run(tc("add_note", text="x"), TRUSTED)
    await tools.run(tc("add_note", text="x"), STRANGER)
    await tools.run(tc("send_followup", channel="sms", message="hi"), TRUSTED)
    await tools.run(tc("made_up"), TRUSTED)
    text = metrics.render()
    assert 'voice_agent_tool_calls_total{tool="add_note",outcome="ok"} 1' in text
    assert 'voice_agent_tool_calls_total{tool="add_note",outcome="error"} 1' in text
    assert 'voice_agent_tool_calls_total{tool="send_followup",outcome="confirm"} 1' in text
    assert 'voice_agent_tool_calls_total{tool="unknown",outcome="error"} 1' in text
    assert 'voice_agent_tool_duration_seconds_count{tool="add_note"} 2' in text


async def test_tool_calls_are_recorded_for_the_audit_trail():
    await tools.run(tc("add_note", text="buy milk"), TRUSTED)
    await tools.run(tc("add_note", text="x"), STRANGER)
    with db.connect() as conn:
        rows = [dict(r) for r in conn.execute(
            "SELECT call_id, tool, args, outcome, output FROM tool_calls ORDER BY id")]
    assert rows == [
        {"call_id": "call-1", "tool": "add_note", "args": '{"text": "buy milk"}',
         "outcome": "ok", "output": "Saved note 1."},
        {"call_id": "call-2", "tool": "add_note", "args": '{"text": "x"}',
         "outcome": "error", "output": "That action isn't available for this caller."},
    ]


async def test_forget_me_needs_confirmation_and_clears_memory():
    from app import memory
    memory.save("+14155550100", ["Their name is Priya.", "They prefer texts."], "c0", "rules")
    first = await tools.run(tc("forget_me"), TRUSTED)
    assert "permanently delete everything I remember" in first["result"]
    assert len(memory.facts_for("+14155550100")) == 2
    done = await tools.run(tc("forget_me", confirmed=True), TRUSTED)
    assert done["result"] == "Done. I deleted 2 things I remembered about you."
    assert memory.facts_for("+14155550100") == []
    assert (await tools.run(tc("forget_me"), STRANGER))["error"].startswith("That action isn't")


async def test_request_transfer_needs_config_and_confirmation(monkeypatch):
    from app.config import get_settings
    call = tc("request_transfer", reason="wants to talk to Sam")
    r = await tools.run(call, TRUSTED)  # confirmation is asked first either way
    call.args["confirmed"] = True
    assert (await tools.run(call, TRUSTED))["error"] == "Call transfers aren't set up."

    monkeypatch.setenv("TRANSFER_NUMBER", "+14155550123")
    get_settings.cache_clear()
    call.args.pop("confirmed")
    assert tools.transfer_approved("call-1") is False
    r = await tools.run(call, TRUSTED)
    assert "transfer this call to the owner's phone" in r["result"]
    assert tools.transfer_approved("call-1") is False  # asking is not approving
    call.args["confirmed"] = True
    assert (await tools.run(call, TRUSTED))["result"].startswith("Transfer approved.")
    assert tools.transfer_approved("call-1") is True
    assert tools.transfer_approved("call-1") is False  # single use
    assert (await tools.run(tc("request_transfer"), STRANGER))["error"].startswith("That action isn't")


def test_transfer_number_must_be_e164(monkeypatch):
    import pytest
    from app.config import load_settings
    monkeypatch.setenv("TRANSFER_NUMBER", "555-1234")
    with pytest.raises(ValueError, match="E.164"):
        load_settings()


async def test_event_times_with_an_offset_are_stored_as_local_time(monkeypatch):
    monkeypatch.setenv("TIMEZONE", "America/Los_Angeles")
    get_settings.cache_clear()
    r = await tools._create_event({"title": "Standup", "starts_at": "2030-01-03T17:00Z"}, TRUSTED)
    assert r == "Added Standup on Thursday January 03 at 09:00."
    await tools._create_event({"title": "Coffee", "starts_at": "2030-01-03T08:30"}, TRUSTED)
    assert await tools._list_events({}, TRUSTED) == ("Coffee on Thursday January 03 at 08:30. "
                                                     "Standup on Thursday January 03 at 09:00.")

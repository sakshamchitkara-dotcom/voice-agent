from app import vapi
from tests.conftest import load_fixture


def test_parses_function_shape_with_object_arguments():
    msg = load_fixture("tool_calls_weather.json")["message"]
    [tc] = vapi.tool_calls(msg)
    assert (tc.id, tc.name, tc.args) == (
        "toolu_01DTPAzUm5Gk3zxrpJ969oMF", "get_weather", {"location": "San Francisco"})


def test_parses_flat_shape_with_parameters():
    [tc] = vapi.tool_calls(load_fixture("tool_calls_search.json")["message"])
    assert (tc.name, tc.args) == ("web_search", {"query": "Vapi voice AI platform"})


def test_parses_string_arguments():
    [tc] = vapi.tool_calls(load_fixture("tool_calls_followup.json")["message"])
    assert tc.args["channel"] == "sms"


def test_result_is_single_line_and_bounded():
    tc = vapi.ToolCall("id1", "x", {})
    r = vapi.result(tc, "a\nb\n\n  c" + "z" * 5000)
    assert r["toolCallId"] == "id1" and "\n" not in r["result"] and len(r["result"]) <= 1800
    assert vapi.error(tc, "boom\nbad") == {"name": "x", "toolCallId": "id1", "error": "boom bad"}


def test_caller_and_type():
    msg = load_fixture("assistant_request.json")["message"]
    assert vapi.caller_number(msg) == "+14155550100"
    assert vapi.call_type(msg) == "inboundPhoneCall"

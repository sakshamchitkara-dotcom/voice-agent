from app import tools, vapi
from scripts import loadtest


def test_percentile_nearest_rank():
    values = [i / 100 for i in range(1, 101)]
    assert loadtest.pct(values, 50) == 0.5
    assert loadtest.pct(values, 95) == 0.95
    assert loadtest.pct(values, 99) == 0.99
    assert loadtest.pct([0.3], 95) == 0.3


def test_payloads_parse_as_vapi_tool_calls_for_real_read_only_tools():
    for tool, samples in loadtest.SAMPLE_ARGS.items():
        assert tool in tools.TOOLS and not tools.TOOLS[tool].agentic
        msg = loadtest.payload(tool, samples[0], 7)["message"]
        (call,) = vapi.tool_calls(msg)
        assert call.name == tool and call.args == samples[0] and call.id.startswith("call_")
        assert vapi.caller_number(msg) == "+15550000007"

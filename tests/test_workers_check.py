from app import vapi
from scripts import workers_check


def test_messages_parse_as_vapi_tool_calls():
    msg = workers_check.message("+15550000001", "convert", {"amount": 5})["message"]
    (call,) = vapi.tool_calls(msg)
    assert call.name == "convert" and call.args == {"amount": 5}
    assert vapi.caller_number(msg) == "+15550000001" and vapi.call_type(msg) == "inboundPhoneCall"


def test_pids_come_from_json_log_lines_only(tmp_path):
    log = tmp_path / "server.log"
    log.write_text('INFO:     Started server process [11]\n'
                   '{"event": "webhook.received", "pid": 11, "request_id": "wc-0"}\n'
                   '{"event": "tool.done", "pid": 12, "request_id": "wc-1"}\n'
                   '{"event": "server.started", "pid": 13}\n')
    assert workers_check.pids_by_request(log) == {"wc-0": 11, "wc-1": 12}

from fastapi.testclient import TestClient

from app import metrics
from app.main import app


def test_histogram_and_counter_render_prometheus_text():
    metrics.reset()
    metrics.http_latency.observe(0.03, method="GET", route="/x")
    metrics.http_latency.observe(3.0, method="GET", route="/x")
    metrics.http_requests.inc(method="GET", route="/x", status="200")
    text = metrics.render()
    assert '# TYPE voice_agent_http_request_duration_seconds histogram' in text
    assert 'voice_agent_http_request_duration_seconds_bucket{method="GET",route="/x",le="0.025"} 0' in text
    assert 'voice_agent_http_request_duration_seconds_bucket{method="GET",route="/x",le="0.05"} 1' in text
    assert 'voice_agent_http_request_duration_seconds_bucket{method="GET",route="/x",le="+Inf"} 2' in text
    assert 'voice_agent_http_request_duration_seconds_count{method="GET",route="/x"} 2' in text
    assert 'voice_agent_http_requests_total{method="GET",route="/x",status="200"} 1' in text


def test_label_values_are_escaped():
    metrics.reset()
    metrics.webhook_messages.inc(type='a"b\nc')
    assert 'voice_agent_webhook_messages_total{type="a\\"b\\nc"} 1' in metrics.render()


def test_metrics_endpoint_counts_requests_by_route_template():
    metrics.reset()
    with TestClient(app) as c:
        c.get("/healthz")
        c.get("/does-not-exist")
        body = c.get("/metrics")
    assert body.headers["content-type"].startswith("text/plain; version=0.0.4")
    assert 'voice_agent_http_requests_total{method="GET",route="/healthz",status="200"} 1' in body.text
    assert 'route="unmatched",status="404"' in body.text

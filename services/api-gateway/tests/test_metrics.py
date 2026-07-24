from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def test_metrics_endpoint_exposes_prometheus_text():
    resp = client.get("/metrics")
    assert resp.status_code == 200
    assert "text/plain" in resp.headers["content-type"]
    body = resp.text
    assert "http_requests_total" in body
    assert "http_request_duration_seconds" in body


def test_request_counter_increments():
    # Drive one request through the app, then read the counter back.
    client.get("/health")
    body = client.get("/metrics").text
    # A 200 /health request must be reflected in the counter samples.
    assert "http_requests_total{" in body
    assert 'status="200"' in body
    assert 'path="/health"' in body

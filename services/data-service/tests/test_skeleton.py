import json

from fastapi.testclient import TestClient

from app.logging import current_trace_id, get_logger
from app.main import app

client = TestClient(app)


def test_health():
    body = client.get("/health").json()
    assert body == {"status": "ok", "service": "data-service"}


def test_trace_id_generated_and_echoed():
    assert client.get("/health").headers.get("X-Trace-Id")
    r = client.get("/health", headers={"X-Trace-Id": "t-1"})
    assert r.headers["X-Trace-Id"] == "t-1"


def test_logger_json_with_contextvar_trace(capsys):
    token = current_trace_id.set("ctx-9")
    try:
        get_logger("data-service").info("hi")
    finally:
        current_trace_id.reset(token)
    rec = json.loads(capsys.readouterr().out.strip())
    assert rec["service"] == "data-service"
    assert rec["trace_id"] == "ctx-9"


def test_metrics_endpoint():
    client.get("/health")
    body = client.get("/metrics").text
    assert "http_requests_total" in body
    assert "config_errors_total" in body
    assert "config_version" in body

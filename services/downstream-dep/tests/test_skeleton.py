import json

from fastapi.testclient import TestClient

from app.logging import current_trace_id, get_logger
from app.main import app

client = TestClient(app)


def test_health():
    assert client.get("/health").json() == {"status": "ok", "service": "downstream-dep"}


def test_trace_id_generated_and_echoed():
    assert client.get("/health").headers.get("X-Trace-Id")
    r = client.get("/health", headers={"X-Trace-Id": "t-2"})
    assert r.headers["X-Trace-Id"] == "t-2"


def test_logger_json_with_contextvar_trace(capsys):
    token = current_trace_id.set("ctx-7")
    try:
        get_logger("downstream-dep").info("hi")
    finally:
        current_trace_id.reset(token)
    rec = json.loads(capsys.readouterr().out.strip())
    assert rec["service"] == "downstream-dep"
    assert rec["trace_id"] == "ctx-7"


def test_metrics_endpoint():
    client.get("/health")
    body = client.get("/metrics").text
    assert "http_requests_total" in body
    assert "downstream_memory_bytes" in body

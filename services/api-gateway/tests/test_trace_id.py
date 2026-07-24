import json

from fastapi.testclient import TestClient

from app.logging import current_trace_id, get_logger
from app.main import app

client = TestClient(app)


def test_generates_trace_id_when_absent():
    resp = client.get("/health")
    assert resp.headers.get("X-Trace-Id")  # present and non-empty


def test_adopts_incoming_trace_id():
    resp = client.get("/health", headers={"X-Trace-Id": "trace-xyz"})
    assert resp.headers["X-Trace-Id"] == "trace-xyz"


def test_logger_falls_back_to_contextvar_trace_id(capsys):
    token = current_trace_id.set("ctx-trace-1")
    try:
        get_logger("api-gateway").info("in request scope")
    finally:
        current_trace_id.reset(token)

    record = json.loads(capsys.readouterr().out.strip())
    assert record["trace_id"] == "ctx-trace-1"

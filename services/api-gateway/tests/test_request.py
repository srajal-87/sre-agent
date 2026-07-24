from prometheus_client import REGISTRY
from fastapi.testclient import TestClient

from app import main
from app.faults import fault_state
from app.main import app

client = TestClient(app)


def setup_function():
    fault_state.clear()


def _timeouts_total():
    return REGISTRY.get_sample_value("upstream_timeouts_total") or 0.0


def test_request_returns_aggregated_upstream_result(monkeypatch):
    async def fake_upstream(trace_id):
        return {"data": "hello", "trace_id": trace_id}

    monkeypatch.setattr(main, "call_upstream", fake_upstream)
    resp = client.get("/request")
    assert resp.status_code == 200
    body = resp.json()
    assert body["service"] == "api-gateway"
    assert body["upstream"]["data"] == "hello"


def test_timeout_fault_returns_504(monkeypatch):
    monkeypatch.setattr(main, "UPSTREAM_TIMEOUT_SECONDS", 0.01)
    fault_state.enable("timeout", {"delay_ms": 3000})
    resp = client.get("/request")
    assert resp.status_code == 504
    assert resp.json()["error"] == "gateway timeout"


def test_timeout_fault_increments_counter(monkeypatch):
    monkeypatch.setattr(main, "UPSTREAM_TIMEOUT_SECONDS", 0.01)
    before = _timeouts_total()
    fault_state.enable("timeout", {"delay_ms": 3000})
    client.get("/request")
    assert _timeouts_total() == before + 1

from prometheus_client import REGISTRY
from fastapi.testclient import TestClient

from app import main
from app.faults import fault_state
from app.main import app

client = TestClient(app)


def setup_function():
    fault_state.clear()


def _config_errors():
    return REGISTRY.get_sample_value("config_errors_total") or 0.0


def test_process_returns_downstream_result(monkeypatch):
    async def fake_downstream(trace_id):
        return {"data": "leaf", "trace_id": trace_id}

    monkeypatch.setattr(main, "call_downstream", fake_downstream)
    resp = client.get("/process")
    assert resp.status_code == 200
    body = resp.json()
    assert body["service"] == "data-service"
    assert body["downstream"]["data"] == "leaf"


def test_bad_config_returns_500():
    fault_state.enable("bad_config", {})
    resp = client.get("/process")
    assert resp.status_code == 500
    assert resp.json()["error"] == "configuration error"


def test_bad_config_increments_counter():
    before = _config_errors()
    fault_state.enable("bad_config", {})
    client.get("/process")
    assert _config_errors() == before + 1


def test_unknown_fault_type_rejected():
    resp = client.post("/admin/fault", json={"fault": "timeout", "enabled": True})
    assert resp.status_code == 400

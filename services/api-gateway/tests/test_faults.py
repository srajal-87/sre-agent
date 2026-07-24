from fastapi.testclient import TestClient

from app.faults import fault_state
from app.main import app

client = TestClient(app)


def setup_function():
    fault_state.clear()  # isolate each test


def test_enable_timeout_fault():
    resp = client.post(
        "/admin/fault",
        json={"fault": "timeout", "enabled": True, "params": {"delay_ms": 3000}},
    )
    assert resp.status_code == 200
    assert fault_state.is_active("timeout") is True
    assert fault_state.params.get("delay_ms") == 3000


def test_disable_fault_via_enabled_false():
    fault_state.enable("timeout", {"delay_ms": 3000})
    resp = client.post("/admin/fault", json={"fault": "timeout", "enabled": False})
    assert resp.status_code == 200
    assert fault_state.is_active("timeout") is False


def test_clear_fault_via_delete():
    fault_state.enable("timeout", {"delay_ms": 3000})
    resp = client.delete("/admin/fault")
    assert resp.status_code == 200
    assert fault_state.is_active("timeout") is False


def test_unknown_fault_type_is_rejected():
    resp = client.post("/admin/fault", json={"fault": "bad_config", "enabled": True})
    assert resp.status_code == 400

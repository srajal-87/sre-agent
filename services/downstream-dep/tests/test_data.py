from prometheus_client import REGISTRY
from fastapi.testclient import TestClient

from app import main
from app.faults import fault_state
from app.main import app

client = TestClient(app)


def setup_function():
    fault_state.clear()


def test_data_returns_payload():
    resp = client.get("/data")
    assert resp.status_code == 200
    body = resp.json()
    assert body["service"] == "downstream-dep"
    assert body["data"] == "ok"


def test_latency_fault_sleeps_configured_delay(monkeypatch):
    calls = []

    async def fake_sleep(seconds):
        calls.append(seconds)

    monkeypatch.setattr(main.asyncio, "sleep", fake_sleep)
    fault_state.enable("latency", {"delay_ms": 250})
    resp = client.get("/data")
    assert resp.status_code == 200
    assert calls == [0.25]


def test_memory_fault_sets_and_clears_gauge():
    client.post(
        "/admin/fault",
        json={"fault": "memory", "enabled": True, "params": {"bytes": 1000}},
    )
    assert REGISTRY.get_sample_value("downstream_memory_bytes") == 1000
    client.post("/admin/fault", json={"fault": "memory", "enabled": False})
    assert REGISTRY.get_sample_value("downstream_memory_bytes") == 0


def test_two_faults_active_independently():
    fault_state.enable("latency", {"delay_ms": 100})
    fault_state.enable("memory", {"bytes": 500})
    assert fault_state.is_active("latency")
    assert fault_state.is_active("memory")


def test_unknown_fault_type_rejected():
    resp = client.post("/admin/fault", json={"fault": "timeout", "enabled": True})
    assert resp.status_code == 400

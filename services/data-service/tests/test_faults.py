from fastapi.testclient import TestClient

from app.faults import fault_state
from app.main import app

client = TestClient(app)


def setup_function():
    fault_state.clear()  # isolate each test


def test_admin_traffic_is_not_counted_in_metrics(capsys):
    """POST /admin/fault is the answer key: it hits only the target service at
    exactly the fault-start instant, so it must leave no metric sample and no
    access-log line for the agent's opening sweep to read."""
    client.post("/admin/fault", json={"fault": "bad_config", "enabled": True})
    client.delete("/admin/fault")
    admin_logs = capsys.readouterr().out

    client.get("/health")  # a normal request must still be counted
    body = client.get("/metrics").text

    assert 'path="/admin/fault"' not in body
    assert "/admin/fault" not in admin_logs
    assert 'path="/health"' in body

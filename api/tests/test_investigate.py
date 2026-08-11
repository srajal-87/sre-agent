import json
import uuid
from pathlib import Path

from fastapi.testclient import TestClient

from app.main import app, get_repository

FIXTURE = Path(__file__).parent / "fixtures" / "alertmanager_firing.json"


class FakeRepository:
    """In-memory stand-in for IncidentRepository, so tests need no database."""

    def __init__(self):
        self.incidents: list[dict] = []

    async def create_incident(self, webhook):
        incident_id = uuid.uuid4()
        investigation_id = uuid.uuid4()
        self.incidents.append(
            {
                "id": incident_id,
                "investigation_id": investigation_id,
                "webhook": webhook,
            }
        )
        return incident_id, investigation_id


def make_client() -> tuple[TestClient, FakeRepository]:
    repo = FakeRepository()
    app.dependency_overrides[get_repository] = lambda: repo
    return TestClient(app), repo


def teardown_function():
    app.dependency_overrides.clear()


def test_investigate_creates_incident_and_stub_investigation():
    client, repo = make_client()

    resp = client.post("/investigate", json=json.loads(FIXTURE.read_text()))

    assert resp.status_code == 201
    body = resp.json()
    assert uuid.UUID(body["incident_id"])
    assert uuid.UUID(body["investigation_id"])
    assert body["status"] == "pending"

    assert len(repo.incidents) == 1
    recorded = repo.incidents[0]["webhook"]
    assert recorded.status == "firing"
    assert recorded.commonLabels["service"] == "api-gateway"
    assert len(recorded.alerts) == 1


def test_investigate_rejects_malformed_payload():
    client, _ = make_client()

    resp = client.post("/investigate", json={"status": "firing", "alerts": []})

    assert resp.status_code == 422

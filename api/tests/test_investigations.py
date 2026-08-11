import json
import uuid
from datetime import datetime, timezone
from pathlib import Path

from fastapi.testclient import TestClient

from app.main import app, get_repository

FIXTURE = Path(__file__).parent / "fixtures" / "alertmanager_firing.json"


class FakeRepository:
    """In-memory stand-in for IncidentRepository, so tests need no database."""

    def __init__(self):
        self.investigations: dict[uuid.UUID, dict] = {}

    async def create_incident(self, webhook):
        incident_id = uuid.uuid4()
        investigation_id = uuid.uuid4()
        now = datetime.now(timezone.utc)
        self.investigations[investigation_id] = {
            "id": investigation_id,
            "incident_id": incident_id,
            "created_at": now,
            "updated_at": now,
            "status": "pending",
            "diagnosis": None,
            "confidence": None,
            "action_taken": None,
            "action_result": None,
            "evidence": None,
            "steps": None,
            "cost_usd": None,
            "latency_ms": None,
            "langsmith_trace_id": None,
            "error": None,
        }
        return incident_id, investigation_id

    async def get_investigation(self, investigation_id):
        return self.investigations.get(investigation_id)


def make_client() -> TestClient:
    app.dependency_overrides[get_repository] = lambda: FakeRepository()
    return TestClient(app)


def teardown_function():
    app.dependency_overrides.clear()


def test_round_trip_returns_pending_stub():
    repo = FakeRepository()
    app.dependency_overrides[get_repository] = lambda: repo
    client = TestClient(app)

    created = client.post("/investigate", json=json.loads(FIXTURE.read_text()))
    investigation_id = created.json()["investigation_id"]

    resp = client.get(f"/investigations/{investigation_id}")

    assert resp.status_code == 200
    body = resp.json()
    assert body["id"] == investigation_id
    assert body["incident_id"] == created.json()["incident_id"]
    assert body["status"] == "pending"
    assert body["diagnosis"] is None
    assert body["confidence"] is None


def test_unknown_id_returns_404():
    client = make_client()

    resp = client.get(f"/investigations/{uuid.uuid4()}")

    assert resp.status_code == 404
    assert resp.json() == {"error": "investigation not found"}


def test_non_uuid_id_returns_422():
    client = make_client()

    resp = client.get("/investigations/not-a-uuid")

    assert resp.status_code == 422

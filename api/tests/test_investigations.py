import asyncio
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

    async def mark_running(self, investigation_id) -> bool:
        return self._update(investigation_id, {"status": "running"})

    async def complete_investigation(self, investigation_id, row: dict) -> bool:
        return self._update(investigation_id, row)

    def _update(self, investigation_id, values: dict) -> bool:
        """False for a row that is not there, like the real UPDATE's rowcount."""
        stored = self.investigations.get(investigation_id)
        if stored is None:
            return False
        stored.update(values)
        stored["updated_at"] = datetime.now(timezone.utc)
        return True


def test_an_investigation_moves_from_pending_to_running_to_completed():
    """The lifecycle the background task drives. Against the fake here; the
    real UPDATE statements are exercised in test_integration_db.py."""
    repo = FakeRepository()
    app.dependency_overrides[get_repository] = lambda: repo
    client = TestClient(app)
    created = client.post("/investigate", json=json.loads(FIXTURE.read_text()))
    investigation_id = uuid.UUID(created.json()["investigation_id"])

    assert asyncio.run(repo.mark_running(investigation_id)) is True
    assert repo.investigations[investigation_id]["status"] == "running"

    assert asyncio.run(
        repo.complete_investigation(
            investigation_id, {"status": "completed", "diagnosis": "d"}
        )
    ) is True

    stored = client.get(f"/investigations/{investigation_id}").json()
    assert stored["status"] == "completed"
    assert stored["diagnosis"] == "d"


def test_writing_to_an_investigation_that_is_not_there_reports_it():
    repo = FakeRepository()

    assert asyncio.run(repo.mark_running(uuid.uuid4())) is False
    assert asyncio.run(repo.complete_investigation(uuid.uuid4(), {})) is False


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

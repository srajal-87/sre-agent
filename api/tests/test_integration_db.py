"""End-to-end smoke test against a real Postgres.

Skipped unless DATABASE_URL is set, so the default unit-test run stays offline.
Run with:  DATABASE_URL=... ../.venv/Scripts/python -m pytest tests/test_integration_db.py
"""

import asyncio
import json
import os
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from app.main import app

FIXTURE = Path(__file__).parent / "fixtures" / "alertmanager_firing.json"

pytestmark = pytest.mark.skipif(
    not os.getenv("DATABASE_URL"), reason="DATABASE_URL not set"
)


def _delete_incident(incident_id: str) -> None:
    """Remove the test row; the investigation goes with it via ON DELETE CASCADE."""

    async def run():
        engine = create_async_engine(os.environ["DATABASE_URL"])
        try:
            async with engine.begin() as conn:
                await conn.execute(
                    text("delete from incidents where id = :id"),
                    {"id": incident_id},
                )
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_investigate_round_trip_against_real_database():
    with TestClient(app) as client:
        created = client.post("/investigate", json=json.loads(FIXTURE.read_text()))
        assert created.status_code == 201
        body = created.json()
        incident_id = body["incident_id"]

        try:
            resp = client.get(f"/investigations/{body['investigation_id']}")
            assert resp.status_code == 200
            investigation = resp.json()
            assert investigation["incident_id"] == incident_id
            assert investigation["status"] == "pending"
            assert investigation["diagnosis"] is None
            assert investigation["created_at"] is not None
        finally:
            _delete_incident(incident_id)


def test_an_investigation_can_be_run_then_completed_in_place():
    """The lifecycle the background task drives: pending -> running ->
    completed, with the report's columns filled in. This is the only place the
    real UPDATE statements are exercised - the endpoint tests use a fake."""
    from app.db import get_sessionmaker
    from app.records import investigation_row
    from app.repository import IncidentRepository

    from agent.graph.state import InvestigationReport

    with TestClient(app) as client:
        created = client.post("/investigate", json=json.loads(FIXTURE.read_text()))
        body = created.json()
        incident_id = body["incident_id"]
        investigation_id = uuid.UUID(body["investigation_id"])

        try:
            repo = IncidentRepository(get_sessionmaker())

            assert asyncio.run(repo.mark_running(investigation_id)) is True
            assert client.get(f"/investigations/{investigation_id}").json()[
                "status"
            ] == "running"

            report = InvestigationReport(
                diagnosis="data-service is serving a corrupted target.",
                fault_type="bad_config", service="data-service", confidence=0.88,
                steps=2, cost_usd=0.0413, latency_ms=37000,
                trace_id=uuid.uuid4(), evidence={"alert": {"alertname": "X"}},
            )
            assert asyncio.run(
                repo.complete_investigation(investigation_id, investigation_row(report))
            ) is True

            stored = client.get(f"/investigations/{investigation_id}").json()
            assert stored["status"] == "completed"
            assert "corrupted target" in stored["diagnosis"]
            assert float(stored["confidence"]) == 0.88
            assert stored["langsmith_trace_id"] == str(report.trace_id)
            assert stored["evidence"]["fault_type"] == "bad_config"
            assert stored["evidence"]["alert"]["alertname"] == "X"
        finally:
            _delete_incident(incident_id)


def test_completing_an_investigation_that_is_not_there_says_so():
    """A background task must be able to tell "written" from "wrote nothing"."""
    from app.db import get_sessionmaker
    from app.repository import IncidentRepository

    repo = IncidentRepository(get_sessionmaker())

    assert asyncio.run(repo.mark_running(uuid.uuid4())) is False

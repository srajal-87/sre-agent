"""End-to-end smoke test against a real Postgres.

Skipped unless DATABASE_URL is set, so the default unit-test run stays offline.
Run with:  DATABASE_URL=... ../.venv/Scripts/python -m pytest tests/test_integration_db.py
"""

import asyncio
import json
import os
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

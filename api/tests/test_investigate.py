import importlib
import json
import os
import uuid
from pathlib import Path

from fastapi.testclient import TestClient

from agent.graph.state import AlertSummary, InvestigationReport
from app import config
from app.main import app, get_investigator, get_repository

FIXTURE = Path(__file__).parent / "fixtures" / "alertmanager_firing.json"


class FakeRepository:
    """In-memory stand-in for IncidentRepository, so tests need no database."""

    def __init__(self):
        self.incidents: list[dict] = []
        self.rows: dict[uuid.UUID, dict] = {}

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
        self.rows[investigation_id] = {"status": "pending"}
        return incident_id, investigation_id

    async def mark_running(self, investigation_id) -> bool:
        return self._update(investigation_id, {"status": "running"})

    async def complete_investigation(self, investigation_id, row: dict) -> bool:
        return self._update(investigation_id, row)

    def _update(self, investigation_id, values: dict) -> bool:
        if investigation_id not in self.rows:
            return False
        self.rows[investigation_id].update(values)
        return True

    @property
    def only_row(self) -> dict:
        return self.rows[self.incidents[0]["investigation_id"]]


class FakeInvestigator:
    """Stands in for agent.graph.investigate. Never touches a model."""

    def __init__(self, report: InvestigationReport | None = None):
        self.calls = []
        self._report = report or InvestigationReport(
            diagnosis="the gateway is timing out on its own calls.",
            fault_type="timeout", service="api-gateway", confidence=0.91,
            steps=2, cost_usd=0.0413, latency_ms=37000, trace_id=uuid.uuid4(),
            evidence={"alert": {"alertname": "GatewayTimeouts"}},
        )

    async def __call__(self, alert, *, incident_id=None, investigation_id=None):
        self.calls.append(
            {"alert": alert, "incident_id": incident_id,
             "investigation_id": investigation_id}
        )
        return self._report


def make_client(investigator=None) -> tuple[TestClient, FakeRepository, object]:
    """A client whose investigator is always a fake - which is the only reason
    it is safe to turn auto-investigation back on (see tests/conftest.py)."""
    repo = FakeRepository()
    investigator = investigator or FakeInvestigator()
    app.dependency_overrides[get_repository] = lambda: repo
    app.dependency_overrides[get_investigator] = lambda: investigator
    config.AGENT_AUTO_INVESTIGATE = True
    return TestClient(app), repo, investigator


def teardown_function():
    app.dependency_overrides.clear()


def _post(client) -> dict:
    resp = client.post("/investigate", json=json.loads(FIXTURE.read_text()))
    assert resp.status_code == 201
    return resp.json()


# -- the endpoint's own contract, unchanged ---------------------------

def test_investigate_creates_incident_and_stub_investigation():
    client, repo, _ = make_client()

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
    client, _, _ = make_client()

    resp = client.post("/investigate", json={"status": "firing", "alerts": []})

    assert resp.status_code == 422


def test_the_response_does_not_wait_for_the_investigation():
    """201 pending is still the answer: an investigation takes a minute or more
    and Alertmanager is not going to hold the connection open for it."""
    client, _, _ = make_client()

    assert _post(client)["status"] == "pending"


# -- the run the endpoint launches ------------------------------------

def test_the_alert_reaches_the_agent_summarised_not_raw():
    """The raw webhook is mostly routing metadata; the graph takes the six
    fields it can reason about."""
    client, _, investigator = make_client()

    _post(client)

    alert = investigator.calls[0]["alert"]
    assert isinstance(alert, AlertSummary)
    assert alert.alertname == "HighErrorRate"
    assert alert.service == "api-gateway"


def test_the_agent_is_told_which_rows_it_is_investigating():
    """Both ids, so the report it returns can be written back to the row that
    is waiting for it - and so the trace is tagged with the incident."""
    client, repo, investigator = make_client()

    body = _post(client)

    call = investigator.calls[0]
    assert str(call["incident_id"]) == body["incident_id"]
    assert str(call["investigation_id"]) == body["investigation_id"]


def test_the_row_is_marked_running_before_the_agent_starts():
    """So a crash mid-investigation is distinguishable from one that was never
    picked up. Both would otherwise read 'pending' forever."""
    seen = []

    class Watching(FakeInvestigator):
        async def __call__(self, alert, **kwargs):
            seen.append(repo.only_row["status"])
            return await super().__call__(alert, **kwargs)

    client, repo, _ = make_client(Watching())

    _post(client)

    assert seen == ["running"]


def test_the_finished_report_is_written_back_to_the_row():
    """The whole point of Stage 4's second row: an investigation that leaves
    something queryable behind."""
    client, repo, _ = make_client()

    _post(client)

    row = repo.only_row
    assert row["status"] == "completed"
    assert "timing out" in row["diagnosis"]
    assert row["confidence"] == 0.91
    assert row["cost_usd"] == 0.0413
    assert row["langsmith_trace_id"]
    assert row["evidence"]["fault_type"] == "timeout"


# -- when the run itself fails ----------------------------------------

class Exploding:
    """An investigator that raises rather than returning a report."""

    def __init__(self, exc: Exception | None = None):
        self._exc = exc or RuntimeError("bedrock is unreachable")

    async def __call__(self, alert, **kwargs):
        raise self._exc


def test_a_run_that_raises_does_not_leave_the_row_stuck_running():
    """The task runs after the response has been sent, so an exception has
    nobody to reach: it would vanish into the event loop and the row would read
    'running' for ever, which is indistinguishable from an investigation still
    in progress."""
    client, repo, _ = make_client(Exploding())

    _post(client)

    assert repo.only_row["status"] == "failed"


def test_a_failed_run_says_what_went_wrong():
    """Readable, and typed: "RuntimeError: bedrock is unreachable" is the whole
    diagnosis available when there is no report."""
    client, repo, _ = make_client(Exploding())

    _post(client)

    error = repo.only_row["error"]
    assert "RuntimeError" in error
    assert "bedrock is unreachable" in error


def test_the_endpoint_still_answers_when_the_run_fails():
    """The alert was recorded either way; that half must not be lost."""
    client, _, _ = make_client(Exploding())

    assert _post(client)["status"] == "pending"


def test_a_row_that_cannot_be_written_does_not_raise_either():
    """The last write is as likely to fail as the run - a dropped pooler
    connection, a deleted incident. There is nowhere left to report it, so the
    task simply ends."""
    class Unwritable(FakeRepository):
        async def complete_investigation(self, investigation_id, row):
            raise RuntimeError("connection reset by peer")

    repo = Unwritable()
    app.dependency_overrides[get_repository] = lambda: repo
    app.dependency_overrides[get_investigator] = lambda: FakeInvestigator()
    config.AGENT_AUTO_INVESTIGATE = True

    assert _post(TestClient(app))["status"] == "pending"


# -- the switch -------------------------------------------------------

def test_auto_investigation_can_be_turned_off():
    """Off, the endpoint is the pending stub it has always been - which is what
    makes a demo stack that records alerts without spending money possible."""
    client, repo, investigator = make_client()
    config.AGENT_AUTO_INVESTIGATE = False

    body = _post(client)

    assert body["status"] == "pending"
    assert investigator.calls == []
    assert repo.only_row["status"] == "pending"


def test_auto_investigation_is_on_by_default():
    """Off by default would mean an endpoint that records alerts and never
    investigates them - which is the half that was already built."""
    saved = os.environ.pop("AGENT_AUTO_INVESTIGATE", None)
    try:
        assert importlib.reload(config).AGENT_AUTO_INVESTIGATE is True
    finally:
        if saved is not None:
            os.environ["AGENT_AUTO_INVESTIGATE"] = saved
        importlib.reload(config)

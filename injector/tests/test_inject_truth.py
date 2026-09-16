"""Ground truth: the incident id, and the row it becomes in Postgres."""

import json
import random
import uuid
from datetime import datetime, timezone

from injector import inject

FIXED = datetime(2026, 8, 20, 14, 9, 30, tzinfo=timezone.utc)


def _run(tmp_path, **overrides):
    """Mirrors test_inject_deploy's helper: one injection, no network, no DB."""
    kwargs = dict(
        fault="latency",
        target="downstream-dep",
        params={"delay_ms": 3000},
        duration=None,
        clear=False,
        poster=lambda url, payload: None,
        sleeper=lambda s: None,
        gt_path=tmp_path / "gt.jsonl",
        # A real ISO timestamp, not a placeholder: this record is mapped into a
        # timestamptz column, so the format is part of what is under test.
        now=lambda: "2026-09-14T11:00:00.000Z",
        deploy=False,
        deploy_clock=lambda: FIXED,
        rng=random.Random(7),
    )
    kwargs.update(overrides)
    return inject.run(**kwargs)


def test_a_caller_supplied_incident_id_is_used_verbatim(tmp_path):
    """run() mints its id inside the record, AFTER the fault has been held for
    its whole duration - but the investigation runs during that sleep and the
    investigations FK needs the id then. So the caller can supply it."""
    record = _run(tmp_path, incident_id="11111111-2222-3333-4444-555555555555")

    assert record["incident_id"] == "11111111-2222-3333-4444-555555555555"

    written = json.loads((tmp_path / "gt.jsonl").read_text(encoding="utf-8").strip())
    assert written["incident_id"] == "11111111-2222-3333-4444-555555555555"


# ── the row ──────────────────────────────────────────────────────────

RECORD = {
    "incident_id": "11111111-2222-3333-4444-555555555555",
    "fault": "memory",
    "target": "downstream-dep",
    "params": {"bytes": 10485760},
    "started_at": "2026-09-14T11:00:00.000Z",
    "ended_at": "2026-09-14T11:03:00.000Z",
    "correlated_deploy": None,
    "deploy_error": None,
}

PAYLOAD = {
    "receiver": "sre-agent",
    "groupKey": '{}:{alertname="HighMemory"}',
    "commonLabels": {"alertname": "HighMemory", "service": "downstream-dep"},
    "alerts": [{"status": "firing"}, {"status": "firing"}],
}


def test_the_incident_row_carries_the_ground_truth_fault_and_target(tmp_path):
    """The ground_truth_* columns have existed since 0001 and are null on every
    row. This is what fills them, so the demo can put the agent's answer and the
    truth side by side."""
    row = inject.incident_row(RECORD, payload=PAYLOAD)

    assert row["ground_truth_fault"] == "memory"
    assert row["ground_truth_target"] == "downstream-dep"
    assert row["service"] == "downstream-dep"
    assert row["source"] == "injector"


def test_the_row_status_satisfies_the_check_constraint():
    """status is `check (status in ('firing', 'resolved'))`. A fault that has
    reverted is resolved; one still running is firing."""
    assert inject.incident_row(RECORD, payload=PAYLOAD)["status"] == "resolved"

    still_running = {**RECORD, "ended_at": None}
    assert inject.incident_row(still_running, payload=PAYLOAD)["status"] == "firing"


def test_the_raw_payload_is_the_webhook_not_the_injector_record():
    """Every other row in that column is an Alertmanager webhook, and
    source='injector' is already the discriminator for these. Putting a
    different shape in there would break any consumer that reads it."""
    row = inject.incident_row(RECORD, payload=PAYLOAD)

    assert row["raw_payload"] == PAYLOAD
    assert row["alert_count"] == 2
    assert row["common_labels"] == PAYLOAD["commonLabels"]
    assert row["receiver"] == "sre-agent"


def test_a_row_with_no_webhook_still_satisfies_the_not_null_columns():
    """`python injector/inject.py` has no scenario payload, and raw_payload and
    alert_count are both NOT NULL."""
    row = inject.incident_row(RECORD)

    assert row["raw_payload"] == {}
    assert row["alert_count"] == 0
    assert row["common_labels"] == {}


def test_the_incident_row_id_is_a_uuid_not_a_string():
    """asyncpg is strict: a str into a uuid column is a DataError at execute
    time, which is to say in the middle of a paid run."""
    row = inject.incident_row(RECORD, payload=PAYLOAD)

    assert isinstance(row["id"], uuid.UUID)
    assert str(row["id"]) == RECORD["incident_id"]


def test_the_incident_row_timestamps_are_datetimes_not_strings():
    """The record holds '...Z' strings from _utc_now_iso, but timestamptz wants
    a datetime - the same way seed_deploys passes deployed_at."""
    row = inject.incident_row(RECORD, payload=PAYLOAD)

    assert isinstance(row["fault_started_at"], datetime)
    assert isinstance(row["fault_ended_at"], datetime)
    assert row["fault_started_at"].tzinfo is not None
    assert row["fault_started_at"] == datetime(2026, 9, 14, 11, 0, tzinfo=timezone.utc)
    assert row["fault_ended_at"].minute == 3


def test_a_fault_that_has_not_ended_has_a_null_end_time():
    row = inject.incident_row({**RECORD, "ended_at": None}, payload=PAYLOAD)

    assert row["fault_ended_at"] is None


# ── the write ────────────────────────────────────────────────────────


def test_the_truth_row_is_written_with_the_scenario_payload(tmp_path):
    written = []

    record = _run(tmp_path, truth_writer=written.append, truth_payload=PAYLOAD)

    (row,) = written
    assert row["ground_truth_fault"] == "latency"
    assert row["raw_payload"] == PAYLOAD
    assert str(row["id"]) == record["incident_id"]
    assert record["truth_error"] is None


def test_a_failing_truth_writer_does_not_abort_the_injection(tmp_path):
    """Mirrors the deploy writer line for line: injecting the fault is the
    point, and the ground truth row is auxiliary. A Postgres outage must not
    stop an experiment."""

    def explode(row):
        raise RuntimeError("could not reach postgres")

    record = _run(tmp_path, truth_writer=explode)

    assert record is not None
    assert record["fault"] == "latency"
    assert "could not reach postgres" in record["truth_error"]


def test_the_ground_truth_line_records_the_truth_error(tmp_path):
    """The JSONL is written LAST, after the truth write has been attempted.
    Write it first and the error is never recorded anywhere."""

    def explode(row):
        raise RuntimeError("could not reach postgres")

    _run(tmp_path, truth_writer=explode)

    line = json.loads((tmp_path / "gt.jsonl").read_text(encoding="utf-8").strip())
    assert "could not reach postgres" in line["truth_error"]


def test_no_truth_writer_means_no_truth_error(tmp_path):
    """The default path - `python injector/inject.py` with no database - still
    records ground truth to the JSONL and reports no error."""
    record = _run(tmp_path)

    assert record["truth_error"] is None


def test_an_incident_id_is_still_minted_when_none_is_given(tmp_path):
    """The CLI has no id to supply, and must keep working unchanged."""
    first = _run(tmp_path)["incident_id"]
    second = _run(tmp_path)["incident_id"]

    assert first and second and first != second

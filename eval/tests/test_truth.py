import json

import pytest

from eval.truth import (
    GROUND_TRUTH_PATH,
    TRUTH_FAULTS,
    GroundTruth,
    find_by_incident_id,
    load_ground_truth,
)

FULL_ROW = {
    "incident_id": "48cfa066-8017-4f5f-a303-9f9999d2e2cd",
    "fault": "latency",
    "target": "downstream-dep",
    "params": {"delay_ms": 3000},
    "started_at": "2026-09-14T11:14:44.337Z",
    "ended_at": "2026-09-14T11:19:45.014Z",
    "correlated_deploy": {
        "service": "downstream-dep",
        "version": "v3.7.1",
        "commit_sha": "c341fc1",
        "author": "j.whitfield",
        "message": "add retry to the downstream client",
        "deployed_at": "2026-09-14T11:13:37.475624+00:00",
    },
    "deploy_error": None,
}


def write_rows(tmp_path, *rows):
    path = tmp_path / "ground_truth.jsonl"
    path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    return path


def test_a_ground_truth_row_loads_into_a_model(tmp_path):
    rows = load_ground_truth(write_rows(tmp_path, FULL_ROW))

    assert len(rows) == 1
    row = rows[0]
    assert isinstance(row, GroundTruth)
    assert row.fault == "latency"
    assert row.target == "downstream-dep"
    assert row.params == {"delay_ms": 3000}
    # Both injector timestamp formats ("...Z" ms, "+00:00" us) parse to aware UTC.
    assert row.started_at.tzinfo is not None
    assert row.started_at.year == 2026 and row.started_at.minute == 14
    assert row.ended_at.hour == 11
    assert row.correlated_deploy["commit_sha"] == "c341fc1"


def test_a_record_missing_every_optional_field_still_loads(tmp_path):
    """The earliest rows predate correlated_deploy/deploy_error, and every row
    written so far predates truth_error. Old rows must stay readable."""
    oldest = {
        "incident_id": "8393bb48-eb4f-4d7c-8597-397ec91d5573",
        "fault": "latency",
        "target": "downstream-dep",
        "params": {"delay_ms": 2000},
        "started_at": "2026-07-23T18:11:14.001Z",
        "ended_at": None,
    }

    row = load_ground_truth(write_rows(tmp_path, oldest))[0]

    assert row.ended_at is None
    assert row.correlated_deploy is None
    assert row.deploy_error is None
    assert row.truth_error is None


def test_an_unknown_fault_label_is_rejected(tmp_path):
    """The incidents migration has no check constraint on ground_truth_fault, so
    a mistyped label would otherwise score as a permanent, silent miss."""
    assert TRUTH_FAULTS == {"timeout", "latency", "bad_config", "memory"}

    typo = {**FULL_ROW, "fault": "laterncy"}

    with pytest.raises(ValueError, match="laterncy"):
        load_ground_truth(write_rows(tmp_path, typo))


def test_a_record_is_found_by_incident_id(tmp_path):
    other = {**FULL_ROW, "incident_id": "aaaaaaaa-0000-0000-0000-000000000000"}
    rows = load_ground_truth(write_rows(tmp_path, other, FULL_ROW))

    found = find_by_incident_id(rows, FULL_ROW["incident_id"])

    assert found is not None
    assert found.incident_id == FULL_ROW["incident_id"]
    # A missing row is not an error here: the scorer voids the run instead.
    assert find_by_incident_id(rows, "no-such-incident") is None


def test_a_truncated_or_blank_line_does_not_kill_the_read(tmp_path):
    """The injector appends to this file and can be killed mid-write, so a
    half-written trailing line is expected. Unparseable text is skipped; a
    well-formed row with a bad label still raises (see the fault vocabulary)."""
    path = tmp_path / "ground_truth.jsonl"
    path.write_text(
        json.dumps(FULL_ROW) + "\n"
        + "\n"
        + '{"incident_id": "dead", "fault": "timeo',
        encoding="utf-8",
    )

    rows = load_ground_truth(path)

    assert [r.incident_id for r in rows] == [FULL_ROW["incident_id"]]


def test_every_row_in_the_repo_ground_truth_loads():
    """Read-only against the real file. Asserts only that every row loads -
    never a count, because the injector appends to it on every run."""
    if not GROUND_TRUTH_PATH.exists():
        pytest.skip("no ground truth recorded in this checkout yet")

    rows = load_ground_truth(GROUND_TRUTH_PATH)

    assert all(r.fault in TRUTH_FAULTS for r in rows)
    assert all(r.started_at.tzinfo is not None for r in rows)

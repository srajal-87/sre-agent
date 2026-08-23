"""The correlated deploy the injector writes before enabling a fault."""

import json
import random
from datetime import datetime, timedelta, timezone

from injector import inject

FIXED = datetime(2026, 8, 20, 14, 9, 30, tzinfo=timezone.utc)

# Same guard as tests/test_seed_deploys.py: both message pools are held to the
# rule that a deploy subject may hint at a cause but never name the fault.
FAULT_WORDS = ("latency", "timeout", "memory", "bad_config", "fault", "inject", "slow")


def _run(tmp_path, **overrides):
    calls = []
    written = []
    kwargs = dict(
        fault="latency",
        target="downstream-dep",
        params={"delay_ms": 3000},
        duration=None,
        clear=False,
        poster=lambda url, payload: calls.append(("fault", payload["enabled"])),
        sleeper=lambda s: None,
        gt_path=tmp_path / "gt.jsonl",
        now=lambda: "T1",
        deploy=True,
        deploy_writer=lambda rows: (
            written.append(rows), calls.append(("deploy", len(rows)))
        ),
        deploy_clock=lambda: FIXED,
        rng=random.Random(7),
    )
    kwargs.update(overrides)
    record = inject.run(**kwargs)
    return record, calls, written


# ── ordering ─────────────────────────────────────────────────────────

def test_the_deploy_is_written_before_the_fault_is_enabled(tmp_path):
    """A deploy recorded after the symptom would not correlate."""
    _, calls, _ = _run(tmp_path)
    assert calls[0][0] == "deploy"
    assert calls[1] == ("fault", True)


# ── the deploy itself ────────────────────────────────────────────────

def test_the_deploy_is_for_the_service_under_fault(tmp_path):
    _, _, written = _run(tmp_path)
    (rows,) = written
    assert len(rows) == 1
    assert rows[0]["service"] == "downstream-dep"


def test_the_deploy_lands_shortly_before_the_fault(tmp_path):
    _, _, written = _run(tmp_path)
    deployed_at = written[0][0]["deployed_at"]

    assert deployed_at < FIXED
    assert FIXED - deployed_at <= timedelta(minutes=inject.DEPLOY_LEAD_MINUTES_MAX)
    assert FIXED - deployed_at >= timedelta(minutes=inject.DEPLOY_LEAD_MINUTES_MIN)


def test_the_deploy_message_never_names_the_fault(tmp_path):
    """The agent must correlate on timing and service, not read the answer."""
    for seed in range(30):
        _, _, written = _run(tmp_path, rng=random.Random(seed))
        message = written[0][0]["message"].lower()
        for word in FAULT_WORDS:
            assert word not in message, message


def test_the_deploy_is_plausible_for_the_fault_area(tmp_path):
    """A latency fault should ship something touching the request path."""
    _, _, written = _run(tmp_path)
    assert written[0][0]["message"] in dict(inject.FAULT_DEPLOY_MESSAGES["latency"])


def test_the_deploy_row_has_the_columns_the_table_requires(tmp_path):
    _, _, written = _run(tmp_path)
    assert set(written[0][0]) == {
        "service", "version", "commit_sha", "author",
        "message", "changed_files", "deployed_at", "status",
    }


def test_an_unknown_fault_still_produces_a_neutral_deploy(tmp_path):
    """New fault types must not crash the injector."""
    _, _, written = _run(tmp_path, fault="disk_full", target="api-gateway")
    assert written[0][0]["service"] == "api-gateway"
    assert written[0][0]["message"]


# ── ground truth ─────────────────────────────────────────────────────

def test_ground_truth_records_the_correlated_deploy(tmp_path):
    record, _, written = _run(tmp_path)
    written_row = written[0][0]

    assert record["correlated_deploy"]["service"] == "downstream-dep"
    assert record["correlated_deploy"]["version"] == written_row["version"]
    assert record["correlated_deploy"]["commit_sha"] == written_row["commit_sha"]

    line = json.loads((tmp_path / "gt.jsonl").read_text().strip())
    assert line["correlated_deploy"]["service"] == "downstream-dep"


def test_ground_truth_records_the_absence_of_a_deploy(tmp_path):
    """Phase 5 must be able to score 'no deploy caused this'."""
    record, calls, written = _run(tmp_path, deploy=False)

    assert written == []
    assert record["correlated_deploy"] is None
    assert calls[0] == ("fault", True)


# ── sometimes no deploy: the required property ───────────────────────

def test_the_default_sometimes_deploys_and_sometimes_does_not(tmp_path):
    """If every fault had a preceding deploy, correlation would be trivial."""
    outcomes = set()
    for seed in range(40):
        record, _, _ = _run(tmp_path, deploy=None, rng=random.Random(seed))
        outcomes.add(record["correlated_deploy"] is None)
    assert outcomes == {True, False}


def test_the_decision_is_reproducible_for_a_given_seed(tmp_path):
    first, _, _ = _run(tmp_path, deploy=None, rng=random.Random(11))
    second, _, _ = _run(tmp_path, deploy=None, rng=random.Random(11))
    assert (first["correlated_deploy"] is None) == (second["correlated_deploy"] is None)


# ── clearing writes nothing ──────────────────────────────────────────

def test_clearing_a_fault_writes_no_deploy(tmp_path):
    record, calls, written = _run(tmp_path, clear=True)

    assert written == []
    assert record is None
    assert calls == [("fault", False)]


# ── a ledger outage must not block fault injection ───────────────────

def test_a_failing_deploy_writer_does_not_abort_the_injection(tmp_path):
    """Injecting the fault is the point; the deploy row is auxiliary."""

    def boom(rows):
        raise RuntimeError("could not connect to the database")

    record, calls, _ = _run(tmp_path, deploy_writer=boom)

    assert calls == [("fault", True)]
    assert record["correlated_deploy"] is None
    assert "could not connect" in record["deploy_error"]

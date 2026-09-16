"""Turning a finished InvestigationReport into an investigations row.

Pure: no database, no session, no network. The row is a plain dict of column
names, which is what makes this testable against the real report model without
anything to connect to.

The split it encodes: a field gets a column only if the migration gave it one.
Everything else - what was ruled out, what the policy gate decided, the token
counts - nests into the ``evidence`` jsonb, flat at its top level so that
``evidence->>'fault_type'`` is a query Phase 5 can write.
"""

import json
from uuid import uuid4

from agent.graph.state import InvestigationReport
from agent.policy.table import PolicyDecision
from app.models import Investigation
from app.records import investigation_row

PROMQL = "rate(http_requests_total[1m])"


def _report(**overrides) -> InvestigationReport:
    fields = {
        "incident_id": uuid4(),
        "investigation_id": uuid4(),
        "trace_id": uuid4(),
        "diagnosis": "data-service is serving a corrupted downstream target.",
        "fault_type": "bad_config",
        "service": "data-service",
        "confidence": 0.88,
        "citations": [PROMQL],
        "ruled_out": ["code change - no deploys in the window"],
        "recommendation": "auto_remediate",
        "policy_decision": PolicyDecision(
            approved=True, recommendation="auto_remediate",
            action="toggle_config", target="data-service",
            blast_radius="low", reason="within policy",
        ),
        "action_taken": "toggle_config(data-service)",
        "action_result": "config toggled; error rate flat within 30s",
        "evidence": {
            "alert": {"alertname": "DataServiceErrors"},
            "results": [{"result": {"query": PROMQL}}],
            "transcript": [{"iteration": 1, "confidence": 0.7}],
        },
        "steps": 2,
        "stop_reason": "confident",
        "status": "completed",
        "llm_calls": 2,
        "input_tokens": 12000,
        "output_tokens": 900,
        "cost_usd": 0.0413,
        "latency_ms": 37000,
        "notes": ["confidence capped at 0.6: no citation resolved"],
    }
    fields.update(overrides)
    return InvestigationReport(**fields)


# -- the columns ------------------------------------------------------

def test_every_key_is_a_real_investigations_column():
    """The drift alarm. A key with no column is a silent TypeError at the
    session, hours after the run that produced it."""
    columns = {column.name for column in Investigation.__table__.columns}

    assert set(investigation_row(_report())) <= columns


def test_the_columns_that_exist_are_filled_from_the_report():
    row = investigation_row(_report())

    assert row["status"] == "completed"
    assert row["confidence"] == 0.88
    assert row["steps"] == 2
    assert row["cost_usd"] == 0.0413
    assert row["latency_ms"] == 37000
    assert row["action_taken"] == "toggle_config(data-service)"
    assert "corrupted downstream target" in row["diagnosis"]


def test_the_trace_id_is_written_as_text():
    """langsmith_trace_id is a text column, not a uuid one."""
    report = _report()

    row = investigation_row(report)

    assert row["langsmith_trace_id"] == str(report.trace_id)


def test_an_untraced_run_leaves_the_trace_column_null():
    assert investigation_row(_report(trace_id=None))["langsmith_trace_id"] is None


# -- what nests into the jsonb ----------------------------------------

def test_the_evidence_the_graph_gathered_survives_whole():
    evidence = investigation_row(_report())["evidence"]

    assert evidence["alert"]["alertname"] == "DataServiceErrors"
    assert evidence["results"][0]["result"]["query"] == PROMQL
    assert evidence["transcript"][0]["iteration"] == 1


def test_the_fields_with_no_column_nest_flat_so_they_can_be_queried():
    """Flat rather than under a wrapper: evidence->>'fault_type' is the shape
    Phase 5 scores against incidents.ground_truth_fault."""
    evidence = investigation_row(_report())["evidence"]

    assert evidence["fault_type"] == "bad_config"
    assert evidence["service"] == "data-service"
    assert evidence["recommendation"] == "auto_remediate"
    assert evidence["stop_reason"] == "confident"
    assert evidence["citations"] == [PROMQL]
    assert evidence["ruled_out"] == ["code change - no deploys in the window"]
    assert evidence["notes"]


def test_the_policy_decision_is_readable_back_out_of_the_jsonb():
    """A denial that cannot be read back is not an audit trail."""
    decision = investigation_row(_report())["evidence"]["policy_decision"]

    assert decision["approved"] is True
    assert decision["rule"] is None or isinstance(decision["rule"], str)
    assert decision["blast_radius"] == "low"


def test_the_bill_is_broken_down_where_the_column_only_has_a_total():
    """cost_usd is one number; the counts behind it are what Phase 5 needs to
    tell an expensive run from a long one."""
    evidence = investigation_row(_report())["evidence"]

    assert evidence["llm_calls"] == 2
    assert evidence["input_tokens"] == 12000
    assert evidence["output_tokens"] == 900


def test_the_whole_row_is_json_serialisable():
    """It goes into a jsonb column; a stray UUID or datetime fails there, not
    here."""
    json.dumps(investigation_row(_report()))


# -- the failed run ---------------------------------------------------

def test_a_failed_run_still_produces_a_row_that_says_why():
    report = _report(
        status="failed", stop_reason="llm_error",
        error="APIConnectionError: no route to host",
        diagnosis="No diagnosis was reached.", policy_decision=None,
        action_taken=None, action_result=None,
    )

    row = investigation_row(report)

    assert row["status"] == "failed"
    assert "no route" in row["error"]
    assert row["evidence"]["stop_reason"] == "llm_error"
    assert row["evidence"]["policy_decision"] is None

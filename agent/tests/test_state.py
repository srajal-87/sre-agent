"""The graph state schema: reducers, control defaults, and the frozen clock.

These tests pin decisions rather than lines of code. The three that matter:

* the accumulating fields must **append**, not overwrite - a node returning
  ``{"evidence": [one_entry]}`` must not wipe the four entries the opening
  sweep gathered;
* ``reference_time`` is frozen once at START, because every window anchors to
  it and an investigation whose windows drift is not reproducible for the eval;
* an ``EvidenceEntry`` holding a ``MetricsResult`` must keep its ``series``
  through ``model_dump`` - that payload is the evidence jsonb column.
"""

import json
from datetime import datetime, timedelta, timezone
from typing import get_type_hints
from uuid import uuid4

import pytest
from pydantic import ValidationError

from agent.graph.state import (
    AlertSummary,
    EvidenceEntry,
    Hypothesis,
    InvestigationState,
    StepRecord,
    ToolCall,
    initial_state,
)
from agent.tools.base import ToolResult
from agent.tools.metrics import MetricSeries, MetricsResult

T0 = datetime(2026, 8, 25, 12, 0, tzinfo=timezone.utc)


def _fixed_now():
    return T0


def _alert(**overrides) -> AlertSummary:
    fields = {
        "alertname": "GatewayTimeouts",
        "service": "api-gateway",
        "severity": "critical",
        "summary": "api-gateway is returning 504s",
        "description": "upstream timeout rate above threshold",
        "started_at": T0,
    }
    fields.update(overrides)
    return AlertSummary(**fields)


def _result(tool="query_metrics", query="up") -> ToolResult:
    return ToolResult(tool=tool, summary="s", source="src", query=query)


# -- reducers ---------------------------------------------------------

def _reducer(field: str):
    """The callable LangGraph will apply for ``field``, or None if there is none.

    Read off ``__metadata__`` rather than ``get_args``: for a plain
    ``Hypothesis | None`` the latter returns the union members, so its first
    element would look like a reducer.
    """
    annotation = get_type_hints(InvestigationState, include_extras=True)[field]
    metadata = getattr(annotation, "__metadata__", ())
    return metadata[0] if metadata else None


@pytest.mark.parametrize(
    "field", ["evidence", "transcript", "seen_calls", "errors", "assistant_turns"]
)
def test_the_accumulating_fields_append_rather_than_replace(field):
    reduce = _reducer(field)
    assert reduce is not None, f"{field} must carry a reducer or a node will wipe it"
    assert reduce(["a"], ["b"]) == ["a", "b"]


@pytest.mark.parametrize("field", ["hypothesis", "pending_tool_calls", "iteration"])
def test_the_overwritten_fields_carry_no_reducer(field):
    """Each cycle replaces these; an append reducer here would be a bug."""
    assert _reducer(field) is None


# -- initial state ----------------------------------------------------

def test_initial_state_sets_the_control_defaults():
    state = initial_state(_alert(), now=_fixed_now)

    assert state["iteration"] == 0
    assert state["status"] == "running"
    assert state["stop_reason"] is None
    assert state["hypothesis"] is None
    assert state["evidence"] == []
    assert state["transcript"] == []
    assert state["seen_calls"] == []
    assert state["assistant_turns"] == []
    assert state["pending_tool_calls"] == []
    assert state["errors"] == []
    assert state["llm_calls"] == 0
    assert state["input_tokens"] == 0
    assert state["output_tokens"] == 0
    assert state["cache_read_tokens"] == 0
    assert state["cost_usd"] == 0.0
    assert state["latency_ms"] == 0


def test_initial_state_carries_the_incident_identifiers():
    incident_id, investigation_id = uuid4(), uuid4()
    state = initial_state(
        _alert(),
        incident_id=incident_id,
        investigation_id=investigation_id,
        now=_fixed_now,
    )

    assert state["incident_id"] == incident_id
    assert state["investigation_id"] == investigation_id


def test_reference_time_is_the_alert_start_when_there_is_one():
    """Windows anchor to the incident, not to when the agent happened to run."""
    started = T0 - timedelta(hours=5)
    state = initial_state(_alert(started_at=started), now=_fixed_now)

    assert state["reference_time"] == started


def test_reference_time_falls_back_to_now_for_an_alert_with_no_start_time():
    state = initial_state(_alert(started_at=None), now=_fixed_now)

    assert state["reference_time"] == T0


def test_started_at_is_the_wall_clock_not_the_reference_time():
    """latency_ms is measured from the run, even for a five-hour-old incident."""
    state = initial_state(_alert(started_at=T0 - timedelta(hours=5)), now=_fixed_now)

    assert state["started_at"] == T0


# -- hypothesis -------------------------------------------------------

def test_a_hypothesis_rejects_a_fault_type_outside_the_closed_enum():
    """The enum is what lets Phase 5 score against ground_truth_fault."""
    with pytest.raises(ValidationError):
        Hypothesis(
            fault_type="cosmic_rays",
            statement="s",
            confidence=0.5,
            rationale="r",
        )


def test_a_hypothesis_defaults_to_no_citations_and_nothing_ruled_out():
    hypothesis = Hypothesis(
        fault_type="unknown", statement="s", confidence=0.1, rationale="r"
    )

    assert hypothesis.service is None
    assert hypothesis.citations == []
    assert hypothesis.ruled_out == []


@pytest.mark.parametrize("sent,expected", [(1.7, 1.0), (-0.2, 0.0)])
def test_an_out_of_range_confidence_is_clamped_not_rejected(sent, expected):
    """Same reasoning as the tools' clamped lookbacks: a silly number from the
    model must not cost a whole turn."""
    hypothesis = Hypothesis(
        fault_type="timeout", statement="s", confidence=sent, rationale="r"
    )

    assert hypothesis.confidence == expected


# -- tool calls and dedupe -------------------------------------------

def test_two_calls_with_the_same_arguments_share_a_signature():
    """Key order and the model's stated reason must not defeat the dedupe."""
    first = ToolCall(
        name="query_metrics",
        arguments={"metric": "http_requests_total", "service": "api-gateway"},
        why="check traffic",
    )
    second = ToolCall(
        name="query_metrics",
        arguments={"service": "api-gateway", "metric": "http_requests_total"},
        why="a completely different stated reason",
    )

    assert first.signature() == second.signature()


def test_a_different_argument_is_a_different_signature():
    first = ToolCall(name="query_logs", arguments={"lookback_minutes": 15})
    second = ToolCall(name="query_logs", arguments={"lookback_minutes": 30})

    assert first.signature() != second.signature()


def test_a_signature_names_its_tool_so_a_transcript_stays_readable():
    call = ToolCall(name="query_logs", arguments={})

    assert call.signature().startswith("query_logs:")


# -- evidence ---------------------------------------------------------

def test_an_evidence_entry_keeps_a_subclassed_result_intact_through_a_dump():
    """Annotating the field as plain ToolResult would silently drop `series`,
    and with it every number the diagnosis is built on."""
    result = MetricsResult(
        tool="query_metrics",
        summary="p99 rose",
        source="prometheus",
        query="histogram_quantile(0.99, ...)",
        series=[MetricSeries(labels={"instance": "api-gateway:8000"}, latest=2.0)],
        series_count=1,
    )
    entry = EvidenceEntry(iteration=1, result=result, requested_because="latency check")

    dumped = entry.model_dump(mode="json")

    assert dumped["result"]["series"][0]["latest"] == 2.0
    json.dumps(dumped)  # this is what lands in the evidence jsonb column


def test_an_evidence_entry_records_a_failed_tool_result_as_evidence():
    """ok=False is an observation, not an error - the state must not blur that."""
    entry = EvidenceEntry(
        iteration=2,
        result=ToolResult(
            tool="query_logs",
            ok=False,
            summary="docker socket unavailable",
            source="docker",
            query="",
            error="permission denied",
        ),
        requested_because="looking for error lines",
    )

    assert entry.result.ok is False


def test_a_step_record_captures_the_cost_of_one_reason_cycle():
    record = StepRecord(
        iteration=1,
        hypothesis=Hypothesis(
            fault_type="timeout", statement="s", confidence=0.72, rationale="r"
        ),
        tool_calls=[ToolCall(name="query_metrics", arguments={"metric": "up"})],
        input_tokens=1200,
        output_tokens=300,
        cache_read_tokens=900,
        latency_ms=4200,
    )

    assert record.confidence == 0.72
    json.dumps(record.model_dump(mode="json"))


def test_a_step_record_with_no_hypothesis_reports_no_confidence():
    """The opening sweep and a failed LLM turn both produce a hypothesis-less step."""
    record = StepRecord(iteration=0)

    assert record.confidence is None

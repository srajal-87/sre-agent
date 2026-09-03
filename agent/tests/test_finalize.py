"""finalize: build the report, do not write it anywhere.

The graph returns a pure object and the caller persists it. That is what keeps
every graph test free of a database, Docker, and a network - the same property
that lets the 3.1 tool suite run offline.

The shape is dictated by the investigations table, which already exists:
``steps`` is an int *count*, not a transcript, so the step-by-step trail nests
inside the ``evidence`` jsonb column alongside the tool results.
"""

import json
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from agent.graph.nodes import finalize
from agent.graph.state import (
    AlertSummary,
    EvidenceEntry,
    Hypothesis,
    InvestigationReport,
    StepRecord,
    ToolCall,
    initial_state,
)
from agent.tools.base import ToolResult
from agent.tools.metrics import MetricSeries, MetricsResult

T0 = datetime(2026, 8, 25, 12, 0, tzinfo=timezone.utc)
PROMQL = "histogram_quantile(0.99, sum by (le, instance) (rate(x[1m])))"

# The columns POST /investigate opens as a pending stub and later fills in.
INVESTIGATION_COLUMNS = {
    "diagnosis",
    "confidence",
    "evidence",
    "steps",
    "cost_usd",
    "latency_ms",
    "status",
    "error",
}


def _hypothesis(**overrides):
    fields = {
        "fault_type": "timeout",
        "service": "api-gateway",
        "statement": "api-gateway's calls to data-service exceed its timeout budget",
        "confidence": 0.91,
        "rationale": "The gateway's own counter rose while data-service stayed flat.",
        "citations": [PROMQL],
        "ruled_out": ["code change - no deploys in the 120m before the alert"],
    }
    fields.update(overrides)
    return Hypothesis(**fields)


def _evidence():
    return [
        EvidenceEntry(
            iteration=0,
            requested_because="is it slow",
            result=MetricsResult(
                tool="query_metrics",
                summary="p99 pinned at 2.0s",
                source="prometheus",
                query=PROMQL,
                series=[MetricSeries(labels={"instance": "api-gateway:8000"}, latest=2.0)],
                series_count=1,
            ),
        ),
        EvidenceEntry(
            iteration=1,
            result=ToolResult(
                tool="query_logs", summary="33x upstream timeout", source="docker",
                query="levels=ERROR lookback=15m",
            ),
        ),
    ]


def _state(**overrides):
    state = initial_state(
        AlertSummary(alertname="GatewayTimeouts", service="api-gateway", started_at=T0),
        incident_id=uuid4(),
        investigation_id=uuid4(),
        now=lambda: T0,
    )
    state.update(
        {
            "iteration": 2,
            "hypothesis": _hypothesis(),
            "evidence": _evidence(),
            "transcript": [
                StepRecord(iteration=1, hypothesis=_hypothesis(confidence=0.72),
                           tool_calls=[ToolCall(name="query_metrics")]),
                StepRecord(iteration=2, hypothesis=_hypothesis()),
            ],
            "stop_reason": "confident",
            "status": "completed",
            "llm_calls": 2,
            "cost_usd": 0.0413,
            "input_tokens": 12000,
            "output_tokens": 900,
        }
    )
    state.update(overrides)
    return state


def _report(state=None, at=T0 + timedelta(seconds=37)) -> InvestigationReport:
    update = finalize(state or _state(), now=lambda: at)
    return update["report"]


# -- the diagnosis ----------------------------------------------------

def test_the_report_carries_the_validated_hypothesis():
    report = _report()

    assert report.fault_type == "timeout"
    assert report.service == "api-gateway"
    assert report.confidence == 0.91


def test_the_diagnosis_reads_as_prose_a_human_can_act_on():
    report = _report()

    assert "timeout budget" in report.diagnosis
    assert "stayed flat" in report.diagnosis


def test_what_was_ruled_out_survives_into_the_report():
    """The negative evidence is half of what makes the diagnosis credible."""
    ruled_out = _report().ruled_out

    assert "no deploys" in ruled_out[0]


def test_the_citations_survive_into_the_report():
    assert _report().citations == [PROMQL]


def test_the_recommendation_is_always_to_escalate_in_this_phase():
    """There are no action tools yet; anything else would be a promise the
    system cannot keep."""
    assert _report().recommendation == "escalate"


# -- the columns ------------------------------------------------------

def test_the_report_fills_every_investigations_column():
    dumped = _report().model_dump()

    assert INVESTIGATION_COLUMNS <= set(dumped)


def test_steps_is_a_count_not_a_transcript():
    """The column is an int; the trail nests inside evidence."""
    report = _report()

    assert report.steps == 2


def test_the_transcript_nests_inside_the_evidence_column():
    evidence = _report().evidence

    assert [step["iteration"] for step in evidence["transcript"]] == [1, 2]


def test_the_confidence_trajectory_is_readable_in_the_trail():
    """0.72 then 0.91 is the story of the investigation."""
    trail = _report().evidence["transcript"]

    assert [step["confidence"] for step in trail] == [0.72, 0.91]


def test_every_tool_result_is_kept_in_full():
    results = _report().evidence["results"]

    assert len(results) == 2
    assert results[0]["result"]["series"][0]["latest"] == 2.0
    assert results[0]["result"]["query"] == PROMQL


def test_the_alert_is_kept_so_the_report_stands_on_its_own():
    assert _report().evidence["alert"]["alertname"] == "GatewayTimeouts"


def test_the_whole_report_round_trips_through_json():
    """It goes into a jsonb column; a stray datetime or UUID would fail there,
    not here."""
    json.dumps(_report().model_dump(mode="json"))


def test_the_identifiers_link_the_report_back_to_its_rows():
    state = _state()

    report = _report(state)

    assert report.incident_id == state["incident_id"]
    assert report.investigation_id == state["investigation_id"]


# -- the bill and the clock -------------------------------------------

def test_the_cost_and_call_count_are_reported():
    report = _report()

    assert report.llm_calls == 2
    assert report.cost_usd == 0.0413
    assert report.input_tokens == 12000


def test_the_latency_is_measured_to_the_end_of_the_run():
    assert _report(at=T0 + timedelta(seconds=37)).latency_ms == 37_000


def test_why_the_run_ended_is_recorded():
    assert _report().stop_reason == "confident"


# -- the unhappy paths ------------------------------------------------

def test_a_failed_run_still_produces_a_readable_report():
    """A timeout or an API error must yield a usable row, not a null one."""
    report = _report(
        _state(
            hypothesis=None,
            status="failed",
            stop_reason="llm_error",
            errors=["iteration 1: boom", "iteration 2: boom"],
        )
    )

    assert report.status == "failed"
    assert report.fault_type == "unknown"
    assert report.confidence == 0.0
    assert report.diagnosis
    assert "boom" in report.error


def test_errors_are_none_when_nothing_went_wrong():
    assert _report().error is None


def test_a_capped_confidence_is_explained_in_the_report():
    report = _report(_state(notes=["iteration 1: confidence capped at 0.6"]))

    assert any("capped" in note for note in report.notes)


def test_an_investigation_with_no_evidence_at_all_still_reports():
    report = _report(_state(hypothesis=None, evidence=[], transcript=[]))

    assert report.steps == 0
    assert report.evidence["results"] == []


def test_finalize_reports_the_status_decide_set():
    update = finalize(_state(status="failed"), now=lambda: T0)

    assert update["status"] == "failed"

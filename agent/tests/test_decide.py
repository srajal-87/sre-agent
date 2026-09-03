"""decide: a pure function of state, and the only place the loop can end.

This is where the confidence score stops being a number the model wrote down
and starts being a claim the code will stand behind. Two mechanisms do that
work, and both are deliberately mechanical:

* a citation that does not resolve to a query some tool actually issued caps
  the confidence, no matter how sure the model says it is;
* "confident" additionally requires two distinct tools to have returned
  ok=True, because one signal is a symptom, not a diagnosis.

The llm_error path routes *through* here rather than jumping from reason to
finalize, so there is exactly one place that ends the loop and exactly one that
sets stop_reason.
"""

from datetime import datetime, timedelta, timezone

import pytest

from agent import config
from agent.graph.nodes import DUPLICATE_ERROR, decide, route
from agent.graph.state import (
    AlertSummary,
    EvidenceEntry,
    Hypothesis,
    StepRecord,
    ToolCall,
    initial_state,
)
from agent.tools.base import ToolResult

T0 = datetime(2026, 8, 25, 12, 0, tzinfo=timezone.utc)

PROMQL = "histogram_quantile(0.99, sum by (le, instance) (rate(x[1m])))"
LOGS = "levels=ERROR,WARNING lookback=15m"


def _entry(tool="query_metrics", query=PROMQL, ok=True, iteration=0, error=None):
    return EvidenceEntry(
        iteration=iteration,
        result=ToolResult(
            tool=tool, ok=ok, summary="s", source="src", query=query, error=error
        ),
    )


def _hypothesis(**overrides):
    fields = {
        "fault_type": "timeout",
        "service": "api-gateway",
        "statement": "the gateway's upstream calls exceed its budget",
        "confidence": 0.91,
        "rationale": "r",
        "citations": [PROMQL],
    }
    fields.update(overrides)
    return Hypothesis(**fields)


def _state(**overrides):
    state = initial_state(
        AlertSummary(alertname="GatewayTimeouts", service="api-gateway", started_at=T0),
        now=lambda: T0,
    )
    state.update(
        {
            "iteration": 1,
            "hypothesis": _hypothesis(),
            "evidence": [_entry(), _entry(tool="query_logs", query=LOGS)],
            "pending_tool_calls": [ToolCall(name="query_metrics", id="toolu_a")],
        }
    )
    state.update(overrides)
    return state


def _decide(state=None, at=T0):
    state = state or _state()
    update = decide(state, now=lambda: at)
    return update, {**state, **update}


# -- carrying on ------------------------------------------------------

def test_an_unfinished_investigation_carries_on():
    update, after = _decide(_state(hypothesis=_hypothesis(confidence=0.5)))

    assert update["stop_reason"] is None
    assert route(after) == "continue"


def test_nothing_is_marked_finished_while_the_loop_continues():
    update, _ = _decide(_state(hypothesis=_hypothesis(confidence=0.5)))

    assert update.get("status", "running") == "running"


# -- confident --------------------------------------------------------

def test_a_well_cited_confident_hypothesis_stops_the_loop():
    update, after = _decide()

    assert update["stop_reason"] == "confident"
    assert update["status"] == "completed"
    assert route(after) == "stop"


def test_confidence_alone_is_not_enough_without_a_second_tool():
    """One signal is a symptom, not a diagnosis."""
    update, _ = _decide(
        _state(evidence=[_entry(), _entry(query="a different promql")])
    )

    assert update["stop_reason"] != "confident"


def test_a_failed_tool_does_not_corroborate():
    update, _ = _decide(
        _state(evidence=[_entry(), _entry(tool="query_logs", query=LOGS, ok=False)])
    )

    assert update["stop_reason"] != "confident"


def test_just_below_the_threshold_carries_on():
    update, _ = _decide(
        _state(hypothesis=_hypothesis(confidence=config.CONFIDENCE_THRESHOLD - 0.01))
    )

    assert update["stop_reason"] is None


# -- citations --------------------------------------------------------

def test_a_citation_that_resolves_leaves_the_confidence_alone():
    update, _ = _decide()

    assert update["hypothesis"].confidence == 0.91


def test_a_citation_that_matches_nothing_caps_the_confidence():
    """The mechanism that makes the score mean something: the code, not the
    model, decides that confidence has been earned."""
    update, _ = _decide(
        _state(hypothesis=_hypothesis(citations=[PROMQL, "rate(invented_metric[5m])"]))
    )

    assert update["hypothesis"].confidence == config.UNCITED_CONFIDENCE_CAP
    assert update["stop_reason"] != "confident"


def test_the_uncited_claim_is_named_in_a_note():
    update, _ = _decide(_state(hypothesis=_hypothesis(citations=["rate(invented[5m])"])))

    assert any("invented" in note for note in update["notes"])


def test_citing_nothing_at_all_caps_the_confidence_too():
    """Otherwise "cite nothing" is a cheaper route to 0.91 than citing badly."""
    update, _ = _decide(_state(hypothesis=_hypothesis(citations=[])))

    assert update["hypothesis"].confidence == config.UNCITED_CONFIDENCE_CAP


def test_a_rewrapped_citation_still_resolves():
    """Long PromQL comes back with different line breaks; that is not a lie."""
    rewrapped = PROMQL.replace(" ", "\n  ")

    update, _ = _decide(_state(hypothesis=_hypothesis(citations=[rewrapped])))

    assert update["hypothesis"].confidence == 0.91


def test_capping_does_not_mutate_the_hypothesis_it_was_given():
    """The transcript already holds this object; editing it would rewrite the
    audit trail after the fact."""
    original = _hypothesis(citations=["rate(invented[5m])"])

    update, _ = _decide(_state(hypothesis=original))

    assert original.confidence == 0.91
    assert update["hypothesis"] is not original


def test_a_capped_confidence_is_never_raised_by_the_cap():
    update, _ = _decide(
        _state(hypothesis=_hypothesis(confidence=0.2, citations=["rate(invented[5m])"]))
    )

    assert update["hypothesis"].confidence == 0.2


# -- the model says it is done ----------------------------------------

def test_requesting_no_tools_ends_the_investigation():
    """A legitimate terminal state even below the threshold: it produces an
    escalation with the confidence actually reached."""
    update, _ = _decide(
        _state(hypothesis=_hypothesis(confidence=0.5), pending_tool_calls=[])
    )

    assert update["stop_reason"] == "model_finished"
    assert update["status"] == "completed"


def test_confident_is_reported_in_preference_to_model_finished():
    update, _ = _decide(_state(pending_tool_calls=[]))

    assert update["stop_reason"] == "confident"


def test_a_single_failed_turn_does_not_read_as_the_model_being_done():
    """A failed call also leaves no pending tools; conflating the two would
    report a crashed run as a finished one."""
    update, _ = _decide(
        _state(
            hypothesis=_hypothesis(confidence=0.5),
            pending_tool_calls=[],
            consecutive_llm_errors=1,
        )
    )

    assert update["stop_reason"] is None


# -- the limits -------------------------------------------------------

def test_two_consecutive_model_failures_end_the_run_as_failed():
    update, after = _decide(
        _state(hypothesis=None, pending_tool_calls=[], consecutive_llm_errors=2)
    )

    assert update["stop_reason"] == "llm_error"
    assert update["status"] == "failed"
    assert route(after) == "stop"


def test_the_iteration_ceiling_stops_a_model_that_would_look_forever():
    update, _ = _decide(
        _state(hypothesis=_hypothesis(confidence=0.5), iteration=config.MAX_ITERATIONS)
    )

    assert update["stop_reason"] == "max_iterations"


def test_the_cost_cap_stops_the_run():
    update, _ = _decide(
        _state(
            hypothesis=_hypothesis(confidence=0.5),
            cost_usd=config.COST_CAP_USD + 0.01,
        )
    )

    assert update["stop_reason"] == "budget_exhausted"


def test_the_wall_clock_cap_stops_the_run():
    state = _state(hypothesis=_hypothesis(confidence=0.5))
    late = T0 + timedelta(seconds=config.WALL_CLOCK_CAP_SECONDS + 1)

    update, _ = _decide(state, at=late)

    assert update["stop_reason"] == "budget_exhausted"


def test_a_stopped_run_records_how_long_it_took():
    update, _ = _decide(_state(), at=T0 + timedelta(seconds=42))

    assert update["latency_ms"] == 42_000


# -- no new evidence --------------------------------------------------

def test_a_turn_that_only_repeated_itself_ends_the_run():
    update, _ = _decide(
        _state(
            hypothesis=_hypothesis(confidence=0.5),
            evidence=[
                _entry(),
                _entry(iteration=1, ok=False, query="", error=DUPLICATE_ERROR),
            ],
        )
    )

    assert update["stop_reason"] == "no_new_evidence"


def test_one_repeat_among_several_new_calls_is_not_a_stop():
    update, _ = _decide(
        _state(
            hypothesis=_hypothesis(confidence=0.5),
            evidence=[
                _entry(iteration=1),
                _entry(iteration=1, ok=False, query="", error=DUPLICATE_ERROR),
            ],
        )
    )

    assert update["stop_reason"] is None


def test_a_confidence_that_has_stopped_moving_ends_the_run():
    """The honest version of "all signals checked": stop when more looking has
    stopped changing the answer."""
    update, _ = _decide(
        _state(
            hypothesis=_hypothesis(confidence=0.52),
            transcript=[
                StepRecord(iteration=1, hypothesis=_hypothesis(confidence=0.50)),
                StepRecord(iteration=2, hypothesis=_hypothesis(confidence=0.52)),
            ],
        )
    )

    assert update["stop_reason"] == "no_new_evidence"


def test_a_confidence_still_climbing_keeps_the_loop_alive():
    update, _ = _decide(
        _state(
            hypothesis=_hypothesis(confidence=0.72),
            transcript=[
                StepRecord(iteration=1, hypothesis=_hypothesis(confidence=0.35)),
                StepRecord(iteration=2, hypothesis=_hypothesis(confidence=0.72)),
            ],
        )
    )

    assert update["stop_reason"] is None


def test_a_single_step_cannot_be_a_plateau():
    update, _ = _decide(
        _state(
            hypothesis=_hypothesis(confidence=0.5),
            transcript=[StepRecord(iteration=1, hypothesis=_hypothesis(confidence=0.5))],
        )
    )

    assert update["stop_reason"] is None


# -- routing ----------------------------------------------------------

@pytest.mark.parametrize(
    "stop_reason,expected",
    [(None, "continue"), ("confident", "stop"), ("llm_error", "stop")],
)
def test_the_router_reads_only_the_stop_reason(stop_reason, expected):
    assert route(_state(stop_reason=stop_reason)) == expected

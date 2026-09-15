"""The policy gate: a pure function, and the only thing that may say yes.

Each test names the rule it exercises, because the rule string is what Phase 5
scores false-autonomous-action rate against.
"""

import json

from agent import config
from agent.graph.state import Hypothesis
from agent.policy import evaluate
from agent.policy.table import RULES


def _hypothesis(**kwargs) -> Hypothesis:
    """A well-formed, well-cited hypothesis that the gate would approve."""
    defaults = dict(
        fault_type="bad_config",
        service="data-service",
        statement="data-service is serving a corrupted downstream target",
        confidence=0.95,
        rationale="config_errors_total climbs from the minute the alert fired",
        citations=["config_errors_total{service='data-service'}"],
        proposed_action="toggle_config",
        action_target="data-service",
    )
    return Hypothesis(**{**defaults, **kwargs})


def _evaluate(hypothesis=None, *, status="completed", stop_reason="confident", **kw):
    return evaluate(
        _hypothesis() if hypothesis is None else hypothesis,
        status=status,
        stop_reason=stop_reason,
        **kw,
    )


# ── approval ─────────────────────────────────────────────────────────

def test_a_confident_well_targeted_low_radius_proposal_is_approved():
    decision = _evaluate()

    assert decision.approved is True
    assert decision.recommendation == "auto_remediate"
    assert decision.rule is None
    assert decision.action == "toggle_config"
    assert decision.target == "data-service"
    assert decision.blast_radius == "low"


def test_restarting_the_leaf_service_is_approved():
    decision = _evaluate(
        _hypothesis(
            fault_type="memory",
            service="downstream-dep",
            proposed_action="restart_service",
            action_target="downstream-dep",
        )
    )

    assert decision.approved is True
    assert decision.blast_radius == "low"


def test_the_reason_reads_as_a_sentence_for_the_report():
    decision = _evaluate()

    assert len(decision.reason) > 20
    decision.reason.encode("ascii")


# ── no_action_proposed ───────────────────────────────────────────────

def test_a_hypothesis_that_proposes_nothing_escalates():
    decision = _evaluate(_hypothesis(proposed_action=None))

    assert decision.approved is False
    assert decision.rule == "no_action_proposed"
    assert decision.recommendation == "escalate"


def test_no_hypothesis_at_all_escalates():
    """A run that failed before forming one still gets a recorded decision."""
    decision = evaluate(None, status="failed", stop_reason="llm_error")

    assert decision.approved is False
    assert decision.rule == "no_action_proposed"
    assert decision.action is None


def test_an_empty_action_string_counts_as_no_proposal():
    decision = _evaluate(_hypothesis(proposed_action="  "))
    assert decision.rule == "no_action_proposed"


# ── unknown_action ───────────────────────────────────────────────────

def test_a_hallucinated_action_is_a_denial_not_a_crash():
    decision = _evaluate(_hypothesis(proposed_action="scale_up"))

    assert decision.approved is False
    assert decision.rule == "unknown_action"
    assert "scale_up" in decision.reason


def test_an_unknown_target_service_is_a_denial():
    decision = _evaluate(
        _hypothesis(service="postgres", action_target="postgres")
    )

    assert decision.rule == "unknown_action"
    assert "postgres" in decision.reason


def test_a_proposal_with_no_target_is_a_denial():
    decision = _evaluate(_hypothesis(action_target=None))

    assert decision.rule == "unknown_action"


# ── unsupported_diagnosis ────────────────────────────────────────────

def test_an_unknown_fault_type_cannot_be_remediated():
    """'unknown' is the agent declining to diagnose; acting on it is guessing."""
    decision = _evaluate(_hypothesis(fault_type="unknown"))

    assert decision.rule == "unsupported_diagnosis"


def test_an_uncited_diagnosis_cannot_be_remediated():
    """decide already caps the confidence; the gate does not rely on that."""
    decision = _evaluate(_hypothesis(citations=[]))

    assert decision.rule == "unsupported_diagnosis"


# ── target_mismatch ──────────────────────────────────────────────────

def test_acting_on_a_service_you_did_not_blame_is_incoherent():
    """The Day 12 failure mode: a loop that names the wrong service."""
    decision = _evaluate(_hypothesis(service="data-service", action_target="api-gateway"))

    assert decision.approved is False
    assert decision.rule == "target_mismatch"
    assert "data-service" in decision.reason and "api-gateway" in decision.reason


def test_blaming_no_service_at_all_is_a_mismatch():
    decision = _evaluate(_hypothesis(service=None))

    assert decision.rule == "target_mismatch"


# ── action_mismatch ──────────────────────────────────────────────────

def test_an_action_that_does_not_address_the_mechanism_is_denied():
    """Every fault here lives in process memory, so a restart would clear a
    config fault - by accident. The gate must not bless that."""
    decision = _evaluate(
        _hypothesis(
            fault_type="bad_config",
            service="downstream-dep",
            proposed_action="restart_service",
            action_target="downstream-dep",
        )
    )

    assert decision.approved is False
    assert decision.rule == "action_mismatch"
    assert "bad_config" in decision.reason


# ── blast_radius ─────────────────────────────────────────────────────

def test_restarting_the_entry_point_is_denied_on_blast_radius():
    decision = _evaluate(
        _hypothesis(
            fault_type="latency",
            service="api-gateway",
            proposed_action="restart_service",
            action_target="api-gateway",
        )
    )

    assert decision.approved is False
    assert decision.rule == "blast_radius"
    assert decision.blast_radius == "high"


def test_restarting_a_service_with_a_dependent_is_denied():
    decision = _evaluate(
        _hypothesis(
            fault_type="memory",
            service="data-service",
            proposed_action="restart_service",
            action_target="data-service",
        )
    )

    assert decision.rule == "blast_radius"
    assert decision.blast_radius == "medium"


def test_a_rollback_is_always_denied():
    """It is in the table precisely so the gate has something it always refuses."""
    for service, fault in (
        ("api-gateway", "timeout"),
        ("data-service", "bad_config"),
        ("downstream-dep", "memory"),
    ):
        decision = _evaluate(
            _hypothesis(
                fault_type=fault,
                service=service,
                proposed_action="rollback_deploy",
                action_target=service,
            )
        )
        assert decision.rule == "blast_radius", service


def test_blast_radius_outranks_confidence():
    """A high-radius action is refused for the durable reason, not this run's score."""
    decision = _evaluate(
        _hypothesis(
            fault_type="latency",
            service="api-gateway",
            proposed_action="restart_service",
            action_target="api-gateway",
            confidence=0.4,
        )
    )

    assert decision.rule == "blast_radius"


# ── incomplete_run ───────────────────────────────────────────────────

def test_a_run_that_ran_out_of_budget_did_not_conclude():
    decision = _evaluate(stop_reason="budget_exhausted")

    assert decision.approved is False
    assert decision.rule == "incomplete_run"
    assert "budget_exhausted" in decision.reason


def test_a_failed_run_never_acts():
    decision = _evaluate(status="failed", stop_reason="llm_error")

    assert decision.rule == "incomplete_run"


def test_a_model_that_simply_stopped_asking_is_not_a_conclusion():
    decision = _evaluate(stop_reason="model_finished")

    assert decision.rule == "incomplete_run"


# ── low_confidence ───────────────────────────────────────────────────

def test_below_the_bar_the_gate_refuses():
    decision = _evaluate(_hypothesis(confidence=0.85))

    assert decision.approved is False
    assert decision.rule == "low_confidence"
    assert "0.85" in decision.reason


def test_the_bar_is_the_config_default_unless_overridden():
    assert _evaluate(_hypothesis(confidence=0.89)).rule == "low_confidence"
    assert _evaluate(_hypothesis(confidence=0.89), min_confidence=0.85).approved is True


def test_the_bar_is_inclusive():
    decision = _evaluate(
        _hypothesis(confidence=config.AUTO_ACTION_CONFIDENCE)
    )
    assert decision.approved is True


# ── the gate is a pure function ──────────────────────────────────────

def test_the_hypothesis_is_not_mutated():
    hypothesis = _hypothesis()
    before = hypothesis.model_dump()

    _evaluate(hypothesis)

    assert hypothesis.model_dump() == before


def test_the_same_input_always_gives_the_same_decision():
    first = _evaluate().model_dump()
    second = _evaluate().model_dump()

    assert first == second


def test_every_denial_names_a_rule_from_the_closed_vocabulary():
    for hypothesis in (
        _hypothesis(proposed_action=None),
        _hypothesis(proposed_action="scale_up"),
        _hypothesis(fault_type="unknown"),
        _hypothesis(action_target="api-gateway"),
        _hypothesis(fault_type="timeout", proposed_action="toggle_config"),
        _hypothesis(confidence=0.1),
    ):
        decision = _evaluate(hypothesis)
        assert decision.rule in RULES


def test_the_decision_survives_a_strict_json_dump_for_the_report():
    json.dumps(_evaluate().model_dump(mode="json"), allow_nan=False)

"""The policy table: pure data, and the invariants that keep it honest."""

import json
import typing

import pytest

from agent.graph.state import FaultType
from agent.policy.table import (
    ACTION_ADDRESSES,
    BLAST_RADIUS,
    RADIUS_LEVELS,
    RULES,
    PolicyDecision,
    blast_radius,
)
from agent.tools.actions import ACTIONS

SERVICES = ("api-gateway", "data-service", "downstream-dep")
FAULT_TYPES = set(typing.get_args(FaultType))


# ── ACTION_ADDRESSES: what each action actually fixes ────────────────

def test_every_registered_action_declares_what_it_addresses():
    """An action with no entry would be silently unusable, or silently universal."""
    assert set(ACTION_ADDRESSES) == set(ACTIONS)


def test_every_addressed_mechanism_is_a_real_fault_type():
    for action, faults in ACTION_ADDRESSES.items():
        assert faults <= FAULT_TYPES, action


def test_no_action_claims_to_address_an_unknown_fault():
    """'unknown' is the agent declining to diagnose; nothing remediates it."""
    for faults in ACTION_ADDRESSES.values():
        assert "unknown" not in faults


def test_no_action_addresses_every_fault_type():
    """A universal fix collapses the gate into a rubber stamp."""
    diagnosable = FAULT_TYPES - {"unknown"}
    for action, faults in ACTION_ADDRESSES.items():
        assert faults != diagnosable, action


def test_config_faults_are_addressed_by_the_config_action():
    assert "bad_config" in ACTION_ADDRESSES["toggle_config"]


def test_a_restart_does_not_claim_to_fix_a_config_error():
    """It would clear it here - every fault is in process memory - but only by
    accident, and the gate must not bless an action for the wrong reason."""
    assert "bad_config" not in ACTION_ADDRESSES["restart_service"]


# ── BLAST_RADIUS: how much damage the action itself can do ───────────

def test_every_action_and_service_pair_resolves_to_a_level():
    """A gap would read as 'not low' and deny for an unexplained reason."""
    for action in ACTIONS:
        for service in SERVICES:
            assert blast_radius(action, service) in RADIUS_LEVELS


def test_an_exact_entry_wins_over_the_wildcard():
    assert blast_radius("restart_service", "api-gateway") == "high"
    assert blast_radius("restart_service", "downstream-dep") == "low"


def test_the_radius_follows_the_dependency_chain():
    """downstream-dep is a leaf; data-service has a dependent; the gateway is the door."""
    assert blast_radius("restart_service", "downstream-dep") == "low"
    assert blast_radius("restart_service", "data-service") == "medium"
    assert blast_radius("restart_service", "api-gateway") == "high"


def test_reconfiguring_is_low_everywhere():
    """No restart, no dropped requests."""
    for service in SERVICES:
        assert blast_radius("toggle_config", service) == "low"


def test_a_rollback_is_high_everywhere():
    """Always denied, on purpose: a gate that never refuses demonstrates nothing."""
    for service in SERVICES:
        assert blast_radius("rollback_deploy", service) == "high"


def test_an_unknown_action_has_no_radius_rather_than_a_safe_looking_one():
    assert blast_radius("scale_up", "data-service") is None


def test_an_unknown_service_has_no_radius_for_a_service_specific_action():
    assert blast_radius("restart_service", "postgres") is None


def test_the_table_is_keyed_by_action_and_service():
    for key in BLAST_RADIUS:
        assert len(key) == 2
        assert BLAST_RADIUS[key] in RADIUS_LEVELS


# ── the rule vocabulary Phase 5 scores against ───────────────────────

def test_the_rules_are_a_stable_ordered_vocabulary():
    assert len(set(RULES)) == len(RULES)
    assert RULES[0] == "no_action_proposed"  # nothing to judge comes first


def test_the_kill_switch_is_not_a_policy_rule():
    """AGENT_ALLOW_WRITES is enforced in run_action, so the gate reaches the same
    verdict either way and a dry run is worth running."""
    assert "writes_disabled" not in RULES


def test_blast_radius_is_judged_before_confidence():
    """'This action is never allowed here' outlives 'not sure enough this time'."""
    assert RULES.index("blast_radius") < RULES.index("low_confidence")


def test_an_action_is_identified_before_it_is_judged():
    """You cannot look up the radius of an action that does not exist."""
    assert RULES.index("unknown_action") < RULES.index("blast_radius")


# ── PolicyDecision ───────────────────────────────────────────────────

def test_an_approval_recommends_auto_remediation():
    decision = PolicyDecision(
        approved=True,
        recommendation="auto_remediate",
        action="toggle_config",
        target="data-service",
        blast_radius="low",
        reason="all policy rules passed",
    )
    assert decision.rule is None


def test_a_denial_carries_a_machine_readable_rule():
    decision = PolicyDecision(
        approved=False,
        recommendation="escalate",
        action="restart_service",
        target="api-gateway",
        blast_radius="high",
        reason="restarting api-gateway is high blast radius",
        rule="blast_radius",
    )
    assert decision.rule == "blast_radius"


def test_a_denial_without_a_rule_is_rejected():
    """Every denial must be scoreable; an unexplained one is a bug, not a state."""
    with pytest.raises(ValueError):
        PolicyDecision(
            approved=False, recommendation="escalate", reason="no", rule=None
        )


def test_an_approval_cannot_recommend_escalation():
    with pytest.raises(ValueError):
        PolicyDecision(approved=True, recommendation="escalate", reason="contradiction")


def test_a_denial_cannot_recommend_acting():
    with pytest.raises(ValueError):
        PolicyDecision(
            approved=False,
            recommendation="auto_remediate",
            reason="contradiction",
            rule="blast_radius",
        )


def test_an_unknown_rule_name_is_rejected():
    """The vocabulary is closed so Phase 5 can score against it mechanically."""
    with pytest.raises(ValueError):
        PolicyDecision(
            approved=False, recommendation="escalate", reason="?", rule="vibes"
        )


def test_the_decision_survives_a_strict_json_dump_for_the_report():
    decision = PolicyDecision(
        approved=False,
        recommendation="escalate",
        reason="no action was proposed",
        rule="no_action_proposed",
    )
    json.dumps(decision.model_dump(mode="json"), allow_nan=False)

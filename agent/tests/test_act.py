"""act: ask the gate, then do exactly what it says. No backends.

The node is thin on purpose - every decision it reports comes from
agent/policy/, and everything it runs comes from the injected executor - so what
these tests pin is the wiring: nothing runs without an approval, a denial is
still recorded, and no failure of an action can lose the investigation.
"""

import asyncio
import json
from datetime import datetime, timezone
from uuid import uuid4

from agent.graph.nodes import act
from agent.graph.state import (
    AlertSummary,
    Hypothesis,
    initial_state,
)
from agent.tools.base import ActionResult

T0 = datetime(2026, 8, 25, 12, 0, tzinfo=timezone.utc)


class Executor:
    """Stands in for run_action."""

    def __init__(self, result=None, error: Exception | None = None, delay=0.0):
        self.calls = []
        self._error = error
        self._delay = delay
        self._result = result

    async def __call__(self, name, arguments, **kwargs):
        self.calls.append((name, arguments))
        if self._delay:
            await asyncio.sleep(self._delay)
        if self._error:
            raise self._error
        return self._result or ActionResult(
            tool=name,
            summary=f"Reset '{arguments['service']}'.",
            source="fake",
            query=f"{name}(service={arguments['service']})",
            target=arguments["service"],
            executed=True,
            verification="confirmed",
        )


def _hypothesis(**overrides):
    fields = {
        "fault_type": "bad_config",
        "service": "data-service",
        "statement": "data-service is serving a corrupted downstream target",
        "confidence": 0.95,
        "rationale": "config_errors_total climbs from the alert onward",
        "citations": ["config_errors_total"],
        "proposed_action": "toggle_config",
        "action_target": "data-service",
    }
    fields.update(overrides)
    return Hypothesis(**fields)


def _state(**overrides):
    state = initial_state(
        AlertSummary(alertname="ConfigErrors", service="data-service", started_at=T0),
        incident_id=uuid4(),
        investigation_id=uuid4(),
        now=lambda: T0,
    )
    state.update(
        {
            "iteration": 2,
            "hypothesis": _hypothesis(),
            "stop_reason": "confident",
            "status": "completed",
        }
    )
    state.update(overrides)
    return state


def _act(state=None, executor=None, **kwargs) -> dict:
    executor = Executor() if executor is None else executor
    return asyncio.run(act(state or _state(), run_action=executor, **kwargs))


# ── approval ─────────────────────────────────────────────────────────

def test_an_approved_action_is_executed_with_the_gates_target():
    executor = Executor()
    update = _act(executor=executor)

    assert executor.calls == [("toggle_config", {"service": "data-service"})]
    assert update["action"].executed is True


def test_the_decision_is_recorded_on_the_state():
    update = _act()

    assert update["policy_decision"].approved is True
    assert update["policy_decision"].action == "toggle_config"


def test_the_action_is_noted_for_the_report():
    notes = _act()["notes"]

    assert any("toggle_config" in note for note in notes)


def test_act_does_not_build_the_report():
    """finalize owns that, and still touches no database."""
    assert "report" not in _act()


# ── denial ───────────────────────────────────────────────────────────

def test_a_denied_action_never_reaches_the_executor():
    executor = Executor()
    state = _state(
        hypothesis=_hypothesis(
            fault_type="latency",
            service="api-gateway",
            proposed_action="restart_service",
            action_target="api-gateway",
        )
    )
    update = _act(state, executor)

    assert executor.calls == []
    assert update["policy_decision"].approved is False
    assert update["policy_decision"].rule == "blast_radius"
    assert update.get("action") is None


def test_a_denial_is_noted_with_its_rule():
    """The note is what a human reads; the rule is what Phase 5 scores."""
    state = _state(hypothesis=_hypothesis(proposed_action=None))
    notes = _act(state)["notes"]

    assert any("no_action_proposed" in note for note in notes)


def test_a_run_that_never_formed_a_hypothesis_still_records_a_decision():
    update = _act(_state(hypothesis=None, status="failed", stop_reason="llm_error"))

    assert update["policy_decision"].approved is False
    assert update["policy_decision"].rule == "no_action_proposed"


def test_the_gate_is_consulted_even_when_writes_are_off():
    """A dry run must still say what it would have done - that is its point."""
    executor = Executor(
        result=ActionResult(
            tool="toggle_config", summary="Dry run: would have run toggle_config.",
            source="fake", query="toggle_config(service=data-service)",
            target="data-service", executed=False, dry_run=True,
        )
    )
    update = _act(executor=executor)

    assert update["policy_decision"].approved is True
    assert update["action"].dry_run is True
    assert update["action"].executed is False


# ── an action can never lose the investigation ───────────────────────

def test_an_executor_that_raises_becomes_a_failed_action_not_a_dead_graph():
    update = _act(executor=Executor(error=RuntimeError("docker exploded")))

    assert update["action"].ok is False
    assert update["action"].executed is False
    assert "docker exploded" in update["action"].error
    assert any("toggle_config" in note for note in update["notes"])


def test_an_action_that_hangs_is_abandoned_rather_than_hanging_the_run():
    update = _act(executor=Executor(delay=0.05), timeout_seconds=0.01)

    assert update["action"].ok is False
    assert "timed out" in update["action"].error.lower()


def test_a_timed_out_action_does_not_claim_nothing_happened():
    """It may well have completed; saying otherwise would be a false record."""
    update = _act(executor=Executor(delay=0.05), timeout_seconds=0.01)

    assert "may" in update["action"].summary.lower()


def test_a_failed_action_still_carries_the_decision():
    update = _act(executor=Executor(error=RuntimeError("boom")))

    assert update["policy_decision"].approved is True


# ── storable ─────────────────────────────────────────────────────────

def test_everything_act_returns_survives_a_strict_json_dump():
    update = _act()

    json.dumps(update["policy_decision"].model_dump(mode="json"), allow_nan=False)
    json.dumps(update["action"].model_dump(mode="json"), allow_nan=False)

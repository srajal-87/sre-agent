"""gather_evidence: an executor, not a decider.

It chooses tools exactly once - the opening sweep - and after that it runs what
``reason`` asked for. Responsibility for "what next" belongs with the node that
has the evidence in front of it.

The invariant that everything else here serves: **every requested call produces
exactly one evidence entry**. We replay the model's assistant turn verbatim, and
the API rejects a tool_use block with no matching tool_result - so a call this
node declines to run (a duplicate, or one over the per-turn cap) still gets an
entry, carrying a result that explains why it was not run.
"""

import asyncio
from datetime import datetime, timezone

from agent import config
from agent.graph.nodes import gather_evidence, opening_sweep
from agent.graph.state import AlertSummary, EvidenceEntry, ToolCall, initial_state
from agent.tools.base import ToolResult

T0 = datetime(2026, 8, 25, 12, 0, tzinfo=timezone.utc)


class Runner:
    """Stands in for run_tool, and records what was asked of it."""

    def __init__(self, *, error=None, ok=True):
        self.calls = []
        self.active = 0
        self.peak_active = 0
        self._error = error
        self._ok = ok

    async def __call__(self, name, arguments, **kwargs):
        self.calls.append((name, arguments))
        self.active += 1
        self.peak_active = max(self.peak_active, self.active)
        await asyncio.sleep(0.01)
        self.active -= 1
        if self._error is not None:
            raise self._error
        return ToolResult(
            tool=name,
            ok=self._ok,
            summary=f"summary for {name}",
            source="fake",
            query=f"query for {name} {sorted(arguments)}",
        )

    @property
    def names(self):
        return [name for name, _ in self.calls]


def _state(**overrides):
    state = initial_state(
        AlertSummary(alertname="GatewayTimeouts", service="api-gateway", started_at=T0),
        now=lambda: T0,
    )
    state.update(overrides)
    return state


def _run(state, runner=None):
    runner = runner or Runner()
    update = asyncio.run(gather_evidence(state, run=runner))
    return update, runner


def _calls(count=1, name="query_logs", **arguments):
    return [
        ToolCall(name=name, arguments={"lookback_minutes": 10 + i, **arguments},
                 id=f"toolu_{i}", why="checking")
        for i in range(count)
    ]


# -- the opening sweep ------------------------------------------------

def test_the_sweep_asks_the_four_questions_an_on_call_asks_first():
    sweep = opening_sweep(AlertSummary(alertname="X", service="api-gateway"), T0)

    assert [call.name for call in sweep] == [
        "query_metrics",
        "query_metrics",
        "query_logs",
        "query_deploy_history",
    ]


def test_the_sweep_asks_whether_traffic_is_flowing_and_whether_it_is_slow():
    sweep = opening_sweep(AlertSummary(alertname="X", service="api-gateway"), T0)

    assert sweep[0].arguments["metric"] == "http_requests_total"
    assert sweep[1].arguments["metric"] == "http_request_duration_seconds"
    assert sweep[1].arguments["aggregation"] == "p99"


def test_the_metric_calls_target_the_alerting_service():
    sweep = opening_sweep(AlertSummary(alertname="X", service="data-service"), T0)

    assert sweep[0].arguments["service"] == "data-service"


def test_an_alert_with_no_service_asks_about_all_of_them():
    """A filter on None would match nothing, and empty reads as "no problem"."""
    sweep = opening_sweep(AlertSummary(alertname="X"), T0)

    assert "service" not in sweep[0].arguments


def test_the_log_sweep_covers_every_service_not_just_the_alerting_one():
    """The cause is usually downstream of the symptom."""
    sweep = opening_sweep(AlertSummary(alertname="X", service="api-gateway"), T0)
    logs = sweep[2].arguments

    assert "service" not in logs
    assert set(logs["levels"]) == {"ERROR", "WARNING"}


def test_the_deploy_sweep_is_anchored_to_the_reference_time():
    """Deploys are correlated against the incident, never against now."""
    sweep = opening_sweep(AlertSummary(alertname="X"), T0)

    assert sweep[3].arguments["reference_time"] == T0.isoformat()


def test_every_sweep_call_says_why_it_was_made():
    for call in opening_sweep(AlertSummary(alertname="X", service="api-gateway"), T0):
        assert call.why


def test_the_sweep_runs_all_four_calls_concurrently():
    """Three different backends - HTTP, a Docker socket, SQL - so they really do
    overlap, and the sweep is on the critical path of every investigation."""
    _, runner = _run(_state())

    assert len(runner.calls) == 4
    assert runner.peak_active == 4


def test_the_sweep_is_not_subject_to_the_per_turn_cap():
    """The cap is token discipline for a model that asks for too much; the sweep
    is four fixed calls that cost no tokens to choose."""
    assert config.MAX_TOOL_CALLS_PER_ITERATION < 4

    update, _ = _run(_state())

    assert len(update["evidence"]) == 4


def test_the_sweep_produces_evidence_tagged_with_iteration_zero():
    update, _ = _run(_state())

    assert [entry.iteration for entry in update["evidence"]] == [0, 0, 0, 0]
    assert all(isinstance(entry, EvidenceEntry) for entry in update["evidence"])


def test_the_sweep_records_a_synthesised_assistant_turn():
    """So that turn-1 evidence has the same shape as every later turn."""
    update, _ = _run(_state())
    turn = update["assistant_turns"][0]

    assert turn.iteration == 0
    assert [block["type"] for block in turn.content] == ["tool_use"] * 4


def test_each_sweep_result_is_linked_to_the_block_that_asked_for_it():
    update, _ = _run(_state())
    turn = update["assistant_turns"][0]

    assert [e.tool_use_id for e in update["evidence"]] == [
        b["id"] for b in turn.content
    ]


def test_the_sweep_registers_what_it_ran_so_it_is_never_repeated():
    update, _ = _run(_state())

    assert len(update["seen_calls"]) == 4
    assert all(isinstance(signature, str) for signature in update["seen_calls"])


# -- executing what reason asked for ----------------------------------

def test_later_iterations_run_the_pending_calls():
    update, runner = _run(_state(iteration=1, pending_tool_calls=_calls(2)))

    assert len(runner.calls) == 2
    assert [entry.iteration for entry in update["evidence"]] == [1, 1]


def test_later_iterations_do_not_synthesise_an_assistant_turn():
    """reason already recorded the real one, thinking blocks and all."""
    update, _ = _run(_state(iteration=1, pending_tool_calls=_calls(1)))

    assert update["assistant_turns"] == []


def test_the_pending_list_is_cleared_once_it_has_been_run():
    update, _ = _run(_state(iteration=1, pending_tool_calls=_calls(1)))

    assert update["pending_tool_calls"] == []


def test_the_stated_reason_is_carried_onto_the_evidence():
    calls = _calls(1)
    calls[0].why = "to falsify the downstream-latency explanation"

    update, _ = _run(_state(iteration=1, pending_tool_calls=calls))

    assert update["evidence"][0].requested_because == calls[0].why


def test_an_empty_pending_list_is_a_no_op_rather_than_an_error():
    """Unreachable - decide would have stopped - but not worth dying over."""
    update, runner = _run(_state(iteration=2, pending_tool_calls=[]))

    assert runner.calls == []
    assert update.get("evidence", []) == []


# -- the invariant ----------------------------------------------------

def test_every_requested_call_produces_exactly_one_entry_even_when_declined():
    """This is what keeps the next API request valid."""
    calls = _calls(5)

    update, _ = _run(_state(iteration=1, pending_tool_calls=calls))

    assert len(update["evidence"]) == 5
    assert [e.tool_use_id for e in update["evidence"]] == [c.id for c in calls]


def test_calls_beyond_the_cap_are_not_run():
    update, runner = _run(_state(iteration=1, pending_tool_calls=_calls(5)))

    assert len(runner.calls) == config.MAX_TOOL_CALLS_PER_ITERATION
    declined = update["evidence"][config.MAX_TOOL_CALLS_PER_ITERATION:]
    assert all(entry.result.ok is False for entry in declined)
    assert all("turn" in entry.result.summary for entry in declined)


def test_a_call_declined_for_the_cap_can_still_be_asked_again_later():
    """Registering it as seen would make it permanently unaskable."""
    calls = _calls(5)

    update, _ = _run(_state(iteration=1, pending_tool_calls=calls))

    assert len(update["seen_calls"]) == config.MAX_TOOL_CALLS_PER_ITERATION


def test_a_repeat_of_an_earlier_call_is_not_run_again():
    calls = _calls(1)

    update, runner = _run(
        _state(
            iteration=1,
            pending_tool_calls=calls,
            seen_calls=[calls[0].signature()],
        )
    )

    assert runner.calls == []
    assert len(update["evidence"]) == 1
    assert update["evidence"][0].result.ok is False
    assert "already" in update["evidence"][0].result.summary.lower()


def test_a_repeat_within_one_turn_is_caught_too():
    call = ToolCall(name="query_logs", arguments={"lookback_minutes": 5}, id="a")
    same = ToolCall(name="query_logs", arguments={"lookback_minutes": 5}, id="b")

    update, runner = _run(_state(iteration=1, pending_tool_calls=[call, same]))

    assert len(runner.calls) == 1
    assert len(update["evidence"]) == 2


# -- failure is evidence, not an exception ----------------------------

def test_a_tool_reporting_ok_false_becomes_evidence():
    update, _ = _run(_state(iteration=1, pending_tool_calls=_calls(1)), Runner(ok=False))

    assert update["evidence"][0].result.ok is False


def test_a_runner_that_raises_is_caught_and_recorded():
    """run_tool guarantees it does not raise; this guards against a bug in our
    own code taking the whole graph down with it."""
    update, _ = _run(
        _state(iteration=1, pending_tool_calls=_calls(1)),
        Runner(error=RuntimeError("boom")),
    )

    entry = update["evidence"][0]
    assert entry.result.ok is False
    assert "boom" in entry.result.error


def test_one_call_raising_does_not_lose_the_others():
    class SometimesRaises(Runner):
        async def __call__(self, name, arguments, **kwargs):
            if arguments.get("lookback_minutes") == 11:
                raise RuntimeError("boom")
            return await Runner.__call__(self, name, arguments, **kwargs)

    update, _ = _run(_state(iteration=1, pending_tool_calls=_calls(3)), SometimesRaises())

    assert len(update["evidence"]) == 3
    assert [e.result.ok for e in update["evidence"]] == [True, False, True]

"""reason: the only node that talks to Claude.

The seam is a FakeModel standing in for ``call_model``, so the whole node - the
retry, the failure paths, the accounting - is exercised with no API key and no
network.

Two behaviours here are load-bearing rather than incidental:

* **Every tool_use block that is not update_hypothesis becomes a pending call**,
  including one naming a tool that does not exist. The executor turns that into
  an ok=False observation the model can read, and - just as importantly - the
  block gets answered, which is what keeps the next request valid.
* **A missing update_hypothesis costs one retry, then carries the previous
  hypothesis forward.** Losing the run over a formatting slip would throw away
  every piece of evidence already gathered.
"""

import asyncio
from datetime import datetime, timezone

from agent.graph.hypothesis import UPDATE_HYPOTHESIS_NAME
from agent.graph.llm import ModelResponse
from agent.graph.nodes import reason
from agent.graph.state import AlertSummary, Hypothesis, initial_state
from agent.prompts.system import SYSTEM_PROMPT

T0 = datetime(2026, 8, 25, 12, 0, tzinfo=timezone.utc)

HYPOTHESIS_INPUT = {
    "fault_type": "timeout",
    "service": "api-gateway",
    "statement": "the gateway's upstream calls exceed its timeout budget",
    "confidence": 0.72,
    "rationale": "p99 sits at the ceiling and the gateway log names data-service.",
    "citations": ["histogram_quantile(0.99, ...)"],
}


def _hypothesis_block(block_id="toolu_h", **overrides):
    return {
        "type": "tool_use",
        "id": block_id,
        "name": UPDATE_HYPOTHESIS_NAME,
        "input": {**HYPOTHESIS_INPUT, **overrides},
    }


def _tool_block(block_id="toolu_a", name="query_metrics", **arguments):
    return {
        "type": "tool_use",
        "id": block_id,
        "name": name,
        "input": arguments or {"metric": "upstream_timeouts_total"},
    }


def _response(content, **overrides):
    fields = {
        "content": content,
        "stop_reason": "tool_use",
        "input_tokens": 1200,
        "output_tokens": 300,
        "cache_read_tokens": 900,
        "cost_usd": 0.01,
        "latency_ms": 4200,
    }
    fields.update(overrides)
    return ModelResponse(**fields)


class FakeModel:
    """Scripted turns, in order. Records what it was asked."""

    def __init__(self, *responses):
        self._responses = list(responses)
        self.requests = []

    async def __call__(self, *, system, tools, messages, **kwargs):
        self.requests.append({"system": system, "tools": tools, "messages": messages})
        if not self._responses:
            return _response([_hypothesis_block()])
        return self._responses.pop(0)


def _state(**overrides):
    state = initial_state(
        AlertSummary(alertname="GatewayTimeouts", service="api-gateway", started_at=T0),
        now=lambda: T0,
    )
    state.update(overrides)
    return state


def _run(state=None, model=None):
    model = model or FakeModel()
    update = asyncio.run(reason(state or _state(), call=model))
    return update, model


# -- the request ------------------------------------------------------

def test_the_system_prompt_is_sent():
    _, model = _run()

    assert model.requests[0]["system"] == SYSTEM_PROMPT


def test_all_four_tools_are_offered():
    _, model = _run()
    names = [tool["name"] for tool in model.requests[0]["tools"]]

    assert UPDATE_HYPOTHESIS_NAME in names
    assert len(names) == 4


def test_the_conversation_is_rebuilt_from_state():
    _, model = _run()

    assert model.requests[0]["messages"][0]["role"] == "user"
    assert "GatewayTimeouts" in model.requests[0]["messages"][0]["content"]


# -- reading the response ---------------------------------------------

def test_the_cycle_counter_advances():
    update, _ = _run(_state(iteration=2))

    assert update["iteration"] == 3


def test_the_hypothesis_is_parsed_off_the_control_tool():
    update, _ = _run()

    assert isinstance(update["hypothesis"], Hypothesis)
    assert update["hypothesis"].fault_type == "timeout"
    assert update["hypothesis"].confidence == 0.72


def test_read_tool_blocks_become_pending_calls_with_their_ids_intact():
    """The id is what links the eventual result back to the block."""
    model = FakeModel(_response([_hypothesis_block(), _tool_block("toolu_a1")]))

    update, _ = _run(model=model)

    assert [call.name for call in update["pending_tool_calls"]] == ["query_metrics"]
    assert update["pending_tool_calls"][0].id == "toolu_a1"
    assert update["pending_tool_calls"][0].arguments == {
        "metric": "upstream_timeouts_total"
    }


def test_the_control_tool_is_not_executed_as_a_read_tool():
    update, _ = _run()

    assert update["pending_tool_calls"] == []


def test_a_call_to_a_tool_that_does_not_exist_is_still_dispatched():
    """run_tool turns it into a readable ok=False observation - and the block
    gets answered, which is what keeps the next request valid."""
    model = FakeModel(_response([_hypothesis_block(), _tool_block("x", "query_traces")]))

    update, _ = _run(model=model)

    assert [call.name for call in update["pending_tool_calls"]] == ["query_traces"]


def test_the_models_stated_reasoning_is_carried_onto_its_calls():
    model = FakeModel(
        _response(
            [
                {"type": "text", "text": "Checking the gateway's own counter."},
                _hypothesis_block(),
                _tool_block("toolu_a1"),
            ]
        )
    )

    update, _ = _run(model=model)

    assert "gateway's own counter" in update["pending_tool_calls"][0].why


def test_the_assistant_turn_is_recorded_verbatim():
    """Thinking blocks must come back to the API unmodified, signature and all."""
    blocks = [
        {"type": "thinking", "thinking": "weighing it up", "signature": "sig"},
        _hypothesis_block(),
    ]
    model = FakeModel(_response(blocks))

    update, _ = _run(_state(iteration=1), model)

    turn = update["assistant_turns"][0]
    assert turn.iteration == 2
    assert turn.content == blocks


# -- the audit trail and the bill -------------------------------------

def test_a_step_is_recorded_for_the_cycle():
    update, _ = _run(_state(iteration=1))
    step = update["transcript"][0]

    assert step.iteration == 2
    assert step.confidence == 0.72
    assert step.input_tokens == 1200
    assert step.cache_read_tokens == 900
    assert step.latency_ms == 4200


def test_the_step_records_what_was_requested():
    model = FakeModel(_response([_hypothesis_block(), _tool_block("toolu_a1")]))

    update, _ = _run(model=model)

    assert [c.name for c in update["transcript"][0].tool_calls] == ["query_metrics"]


def test_the_totals_accumulate_rather_than_replace():
    update, _ = _run(
        _state(llm_calls=1, input_tokens=500, output_tokens=100, cost_usd=0.02)
    )

    assert update["llm_calls"] == 2
    assert update["input_tokens"] == 1700
    assert update["output_tokens"] == 400
    assert update["cost_usd"] == 0.03


# -- a missing hypothesis ---------------------------------------------

def test_a_turn_without_a_hypothesis_is_retried_once():
    model = FakeModel(
        _response([_tool_block("toolu_a1")]),
        _response([_hypothesis_block(confidence=0.9)]),
    )

    update, _ = _run(model=model)

    assert len(model.requests) == 2
    assert update["hypothesis"].confidence == 0.9


def test_the_retry_says_plainly_what_was_missing():
    model = FakeModel(_response([_tool_block("toolu_a1")]))

    _, model = _run(model=model)

    nudge = model.requests[1]["messages"][-1]
    assert nudge["role"] == "user"
    assert UPDATE_HYPOTHESIS_NAME in nudge["content"]


def test_a_malformed_hypothesis_counts_as_missing():
    """An out-of-enum fault_type is not coerced - it is asked for again."""
    model = FakeModel(
        _response([_hypothesis_block(fault_type="cpu")]),
        _response([_hypothesis_block()]),
    )

    update, _ = _run(model=model)

    assert len(model.requests) == 2
    assert update["hypothesis"].fault_type == "timeout"


def test_after_a_failed_retry_the_previous_hypothesis_is_carried_forward():
    """Losing the run over a formatting slip would throw away the evidence."""
    previous = Hypothesis(
        fault_type="latency", statement="s", confidence=0.4, rationale="r"
    )
    model = FakeModel(
        _response([_tool_block("toolu_a1")]), _response([_tool_block("toolu_a2")])
    )

    update, _ = _run(_state(hypothesis=previous), model)

    assert update["hypothesis"] is previous
    assert any(UPDATE_HYPOTHESIS_NAME in error for error in update["errors"])


def test_both_attempts_are_paid_for():
    model = FakeModel(
        _response([_tool_block("toolu_a1")]), _response([_hypothesis_block()])
    )

    update, _ = _run(model=model)

    assert update["llm_calls"] == 2
    assert update["cost_usd"] == 0.02


def test_the_last_attempts_calls_are_the_ones_dispatched():
    model = FakeModel(
        _response([_tool_block("toolu_a1")]),
        _response([_hypothesis_block(), _tool_block("toolu_a2")]),
    )

    update, _ = _run(model=model)

    assert [c.id for c in update["pending_tool_calls"]] == ["toolu_a2"]


# -- failure ----------------------------------------------------------

def test_an_api_failure_is_recorded_and_counted():
    model = FakeModel(ModelResponse(ok=False, error="RuntimeError: boom", latency_ms=10))

    update, _ = _run(model=model)

    assert update["consecutive_llm_errors"] == 1
    assert any("boom" in error for error in update["errors"])
    assert update["pending_tool_calls"] == []
    assert update["assistant_turns"] == []


def test_a_failed_turn_still_advances_the_cycle_counter():
    """Otherwise max_iterations can never fire and the loop runs forever."""
    model = FakeModel(ModelResponse(ok=False, error="boom"))

    update, _ = _run(_state(iteration=1), model)

    assert update["iteration"] == 2


def test_a_failed_turn_leaves_the_hypothesis_untouched():
    previous = Hypothesis(
        fault_type="latency", statement="s", confidence=0.4, rationale="r"
    )
    model = FakeModel(ModelResponse(ok=False, error="boom"))

    update, _ = _run(_state(hypothesis=previous), model)

    assert "hypothesis" not in update or update["hypothesis"] is previous


def test_consecutive_failures_accumulate():
    model = FakeModel(ModelResponse(ok=False, error="boom"))

    update, _ = _run(_state(consecutive_llm_errors=1), model)

    assert update["consecutive_llm_errors"] == 2


def test_a_good_turn_clears_the_failure_streak():
    update, _ = _run(_state(consecutive_llm_errors=1))

    assert update["consecutive_llm_errors"] == 0


def test_a_refusal_is_treated_as_a_failed_turn():
    """A refusal is a 200 with no tool calls, which is otherwise indistinguishable
    from "I am done"."""
    model = FakeModel(
        _response([], stop_reason="refusal", refusal="cyber: declined")
    )

    update, _ = _run(model=model)

    assert update["consecutive_llm_errors"] == 1
    assert any("cyber" in error for error in update["errors"])


def test_an_unexpected_stop_reason_is_noted_but_the_content_is_still_used():
    """A truncated turn that still carries a usable hypothesis is worth keeping."""
    model = FakeModel(_response([_hypothesis_block()], stop_reason="max_tokens"))

    update, _ = _run(model=model)

    assert update["hypothesis"].fault_type == "timeout"
    assert any("max_tokens" in error for error in update["errors"])
    assert update["consecutive_llm_errors"] == 0

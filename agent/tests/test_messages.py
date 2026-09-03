"""Assembling what the API actually receives.

The message list is rebuilt from state on every turn rather than accumulated in
place, so that older evidence can collapse to its summary line. That costs
nothing: the cache breakpoint sits on the last tool definition, so the cached
prefix is tools + system and the messages were never cached anyway.

The load-bearing tests are the two structural ones. The Anthropic API rejects a
request where a tool_use block has no matching tool_result, and we replay the
model's own assistant turns verbatim - so *every* tool_use the model emitted
must be answered, including update_hypothesis, and including calls the executor
declined to run. That invariant is what forbids gather_evidence from silently
dropping a duplicate.
"""

import json
from datetime import datetime, timezone

from agent.graph.hypothesis import UPDATE_HYPOTHESIS_NAME
from agent.graph.messages import build_messages, synthetic_assistant_turn
from agent.graph.state import (
    AlertSummary,
    AssistantTurn,
    EvidenceEntry,
    ToolCall,
    initial_state,
)
from agent.tools.base import ToolResult
from agent.tools.metrics import MetricSeries, MetricsResult

T0 = datetime(2026, 8, 25, 12, 0, tzinfo=timezone.utc)


def _tool_use(block_id, name="query_metrics", arguments=None):
    return {"type": "tool_use", "id": block_id, "name": name, "input": arguments or {}}


def _entry(iteration, tool_use_id, *, tool="query_metrics", detail="rows"):
    return EvidenceEntry(
        iteration=iteration,
        tool_use_id=tool_use_id,
        requested_because="because",
        result=MetricsResult(
            tool=tool,
            summary=f"summary for {tool_use_id}",
            source="prometheus",
            query=f"promql for {tool_use_id}",
            series=[MetricSeries(labels={"instance": detail}, latest=1.0)],
            series_count=1,
        ),
    )


def _state(**overrides):
    state = initial_state(
        AlertSummary(alertname="GatewayTimeouts", service="api-gateway"), now=lambda: T0
    )
    state.update(overrides)
    return state


def _two_iteration_state():
    """The sweep, one reason turn that asked for more, and its results."""
    return _state(
        assistant_turns=[
            AssistantTurn(iteration=0, content=[_tool_use("toolu_seed_0")]),
            AssistantTurn(
                iteration=1,
                content=[
                    {"type": "thinking", "thinking": "weighing it up", "signature": "s"},
                    {"type": "text", "text": "Checking the gateway's own counter."},
                    _tool_use("toolu_h1", UPDATE_HYPOTHESIS_NAME, {"confidence": 0.72}),
                    _tool_use("toolu_a1"),
                ],
            ),
        ],
        evidence=[
            _entry(0, "toolu_seed_0", detail="from-the-sweep"),
            _entry(1, "toolu_a1", detail="from-turn-one"),
        ],
    )


def _roles(messages):
    return [message["role"] for message in messages]


def _blocks(messages, role, index):
    """The content blocks of the index-th message with this role."""
    return [m for m in messages if m["role"] == role][index]["content"]


# -- overall shape ----------------------------------------------------

def test_the_first_message_is_the_alert_brief():
    """The system prompt holds nothing per-run, so this is where the model
    learns what it is investigating."""
    messages = build_messages(_state())

    assert messages[0]["role"] == "user"
    assert "GatewayTimeouts" in messages[0]["content"]


def test_the_roles_alternate_strictly():
    assert _roles(build_messages(_two_iteration_state())) == [
        "user",
        "assistant",
        "user",
        "assistant",
        "user",
    ]


def test_no_message_is_ever_empty():
    """The API rejects an empty content block, and an empty turn is always a
    bug in the assembly rather than something the model did."""
    for message in build_messages(_two_iteration_state()):
        assert message["content"]


def test_the_message_list_is_json_serialisable():
    json.dumps(build_messages(_two_iteration_state()))


def test_assistant_turns_are_replayed_in_iteration_order():
    state = _two_iteration_state()
    state["assistant_turns"] = list(reversed(state["assistant_turns"]))

    messages = build_messages(state)

    assert _blocks(messages, "assistant", 0)[0]["id"] == "toolu_seed_0"


# -- the invariant that keeps the request valid ------------------------

def test_every_tool_use_is_answered_in_the_very_next_user_message():
    messages = build_messages(_two_iteration_state())

    for assistant, following in zip(messages, messages[1:]):
        if assistant["role"] != "assistant":
            continue
        requested = [b["id"] for b in assistant["content"] if b["type"] == "tool_use"]
        answered = [
            b["tool_use_id"]
            for b in following["content"]
            if isinstance(b, dict) and b.get("type") == "tool_result"
        ]
        assert requested == answered


def test_the_control_tool_gets_a_tool_result_of_its_own():
    """update_hypothesis returns nothing useful, but the API still demands an
    answer for its block."""
    results = _blocks(build_messages(_two_iteration_state()), "user", 2)
    recorded = [r for r in results if r["tool_use_id"] == "toolu_h1"]

    assert len(recorded) == 1
    assert "record" in recorded[0]["content"].lower()


def test_a_tool_use_with_no_recorded_evidence_still_gets_an_answer():
    """Defensive: this should be unreachable, but the failure mode it prevents
    is a 400 that loses the whole run."""
    state = _state(
        assistant_turns=[AssistantTurn(iteration=0, content=[_tool_use("toolu_lost")])],
        evidence=[],
    )

    results = _blocks(build_messages(state), "user", 1)

    assert results[0]["tool_use_id"] == "toolu_lost"
    assert results[0]["content"]


# -- summary-first --------------------------------------------------

def test_the_newest_iterations_evidence_is_rendered_in_full():
    results = _blocks(build_messages(_two_iteration_state()), "user", 2)
    rendered = {r["tool_use_id"]: r["content"] for r in results}

    assert "from-turn-one" in rendered["toolu_a1"]


def test_older_evidence_collapses_to_its_summary_line():
    results = _blocks(build_messages(_two_iteration_state()), "user", 1)
    rendered = results[0]["content"]

    assert "from-the-sweep" not in rendered  # the rows are gone
    assert "summary for toolu_seed_0" in rendered
    assert "promql for toolu_seed_0" in rendered  # the citation is not


def test_the_sweeps_own_evidence_is_detailed_while_it_is_the_newest():
    """On the first reason call there is nothing older to collapse."""
    state = _state(
        assistant_turns=[AssistantTurn(iteration=0, content=[_tool_use("toolu_seed_0")])],
        evidence=[_entry(0, "toolu_seed_0", detail="from-the-sweep")],
    )

    results = _blocks(build_messages(state), "user", 1)

    assert "from-the-sweep" in results[0]["content"]


# -- thinking blocks --------------------------------------------------

def test_the_latest_assistant_turn_keeps_its_thinking_blocks_verbatim():
    """Extended thinking is on, and the API requires the thinking block that
    preceded a tool_use to come back unmodified, signature included."""
    blocks = _blocks(build_messages(_two_iteration_state()), "assistant", 1)
    thinking = [b for b in blocks if b["type"] == "thinking"]

    assert thinking == [
        {"type": "thinking", "thinking": "weighing it up", "signature": "s"}
    ]


def test_earlier_assistant_turns_drop_their_thinking_blocks():
    """Previous turns do not need them, and they are pure tokens."""
    state = _two_iteration_state()
    state["assistant_turns"][0] = AssistantTurn(
        iteration=0,
        content=[
            {"type": "thinking", "thinking": "old", "signature": "s"},
            _tool_use("toolu_seed_0"),
        ],
    )

    blocks = _blocks(build_messages(state), "assistant", 0)

    assert [b["type"] for b in blocks] == ["tool_use"]


def test_an_older_turn_of_nothing_but_thinking_keeps_it():
    """Stripping to nothing would send an empty assistant turn, which is a 400."""
    state = _state(
        assistant_turns=[
            AssistantTurn(
                iteration=0,
                content=[{"type": "thinking", "thinking": "only this", "signature": "s"}],
            ),
            AssistantTurn(iteration=1, content=[_tool_use("toolu_a1")]),
        ],
        evidence=[_entry(1, "toolu_a1")],
    )

    assert _blocks(build_messages(state), "assistant", 0)


def test_text_blocks_are_kept_on_every_turn():
    state = _two_iteration_state()

    blocks = _blocks(build_messages(state), "assistant", 1)

    assert any(b["type"] == "text" for b in blocks)


# -- the synthesised opening sweep ------------------------------------

def test_the_sweep_is_synthesised_as_an_ordinary_assistant_turn():
    """Fork 9: one uniform representation for all evidence, at the price of
    fabricating tool_use ids."""
    calls = [
        ToolCall(name="query_metrics", arguments={"metric": "http_requests_total"}),
        ToolCall(name="query_logs", arguments={"levels": ["ERROR"]}),
    ]

    turn = synthetic_assistant_turn(0, calls)

    assert turn.iteration == 0
    assert [b["type"] for b in turn.content] == ["tool_use", "tool_use"]
    assert turn.content[0]["name"] == "query_metrics"
    assert turn.content[0]["input"] == {"metric": "http_requests_total"}


def test_the_synthesised_turn_assigns_each_call_a_unique_id():
    calls = [ToolCall(name="query_logs"), ToolCall(name="query_logs")]

    turn = synthetic_assistant_turn(0, calls)
    ids = [block["id"] for block in turn.content]

    assert len(set(ids)) == 2
    assert all(i.startswith("toolu_") for i in ids)


def test_the_synthesised_turn_writes_its_ids_back_onto_the_calls():
    """gather_evidence tags each EvidenceEntry with the id, which is the only
    thing that later links a result to the block that asked for it."""
    calls = [ToolCall(name="query_logs")]

    turn = synthetic_assistant_turn(0, calls)

    assert calls[0].id == turn.content[0]["id"]


def test_a_failed_result_is_rendered_as_an_ordinary_observation():
    state = _state(
        assistant_turns=[AssistantTurn(iteration=0, content=[_tool_use("toolu_seed_0")])],
        evidence=[
            EvidenceEntry(
                iteration=0,
                tool_use_id="toolu_seed_0",
                result=ToolResult(
                    tool="query_logs",
                    ok=False,
                    summary="Could not reach the Docker socket.",
                    source="docker",
                    query="",
                    error="permission denied",
                ),
            )
        ],
    )

    results = _blocks(build_messages(state), "user", 1)

    assert "permission denied" in results[0]["content"]
    assert results[0].get("is_error") is not True  # a broken tool, not a broken call

"""One model call, priced and accounted for.

The client is injected, so every test here runs with no API key and no network.
``call_model`` never raises for the same reason a tool never raises: the caller
is a graph, and an exception thirty seconds into an investigation loses the
whole run, evidence included.

The cost tests exist because a wrong constant here is invisible - the number
still looks like a number - and it is what the Phase 5 eval reports.
"""

import asyncio
import json
from types import SimpleNamespace

import pytest

from agent import config
from agent.graph.hypothesis import UPDATE_HYPOTHESIS_NAME
from agent.graph.llm import call_model, estimate_cost, tool_definitions
from agent.tools import TOOLS

READ_TOOLS = {"query_metrics", "query_logs", "query_deploy_history"}


def _usage(**overrides):
    usage = {
        "input_tokens": 1000,
        "output_tokens": 500,
        "cache_read_input_tokens": 10000,
        "cache_creation_input_tokens": 2000,
    }
    usage.update(overrides)
    return SimpleNamespace(**usage)


def _response(**overrides):
    fields = {
        "content": [{"type": "text", "text": "looking into it"}],
        "stop_reason": "tool_use",
        "stop_details": None,
        "model": "claude-opus-5",
        "usage": _usage(),
    }
    fields.update(overrides)
    return SimpleNamespace(**fields)


class _FakeMessages:
    def __init__(self, response=None, error=None):
        self._response = response
        self._error = error
        self.kwargs = None

    async def create(self, **kwargs):
        self.kwargs = kwargs
        if self._error is not None:
            raise self._error
        return self._response


class FakeClient:
    """Stands in for anthropic.AsyncAnthropicBedrock - the whole testing seam."""

    def __init__(self, response=None, error=None):
        self.messages = _FakeMessages(response, error)


def _call(client, **overrides):
    kwargs = {"system": "be an SRE", "tools": tool_definitions(), "messages": []}
    kwargs.update(overrides)
    return asyncio.run(call_model(client=client, **kwargs))


# -- the tool list --------------------------------------------------

def test_the_model_is_offered_the_three_read_tools_plus_the_control_tool():
    names = [tool["name"] for tool in tool_definitions()]

    assert set(names) == READ_TOOLS | {UPDATE_HYPOTHESIS_NAME}


def test_the_control_tool_comes_last_so_the_breakpoint_covers_everything():
    assert tool_definitions()[-1]["name"] == UPDATE_HYPOTHESIS_NAME


def test_the_order_is_deterministic():
    """Tool order is part of the cache prefix; dict order is not a contract."""
    assert [t["name"] for t in tool_definitions()] == [
        t["name"] for t in tool_definitions()
    ]
    assert [t["name"] for t in tool_definitions()][:-1] == sorted(READ_TOOLS)


def test_only_the_last_tool_carries_the_cache_breakpoint():
    """One breakpoint covers tools + system, which is the whole stable prefix."""
    tools = tool_definitions()

    assert tools[-1]["cache_control"] == {"type": "ephemeral"}
    assert all("cache_control" not in tool for tool in tools[:-1])


def test_caching_can_be_turned_off():
    assert all("cache_control" not in tool for tool in tool_definitions(cache=False))


def test_every_definition_has_the_three_api_keys_and_serialises():
    for tool in tool_definitions(cache=False):
        assert set(tool) == {"name", "description", "input_schema"}
    json.dumps(tool_definitions())


def test_the_read_tool_schemas_come_straight_from_the_registry():
    metrics = next(t for t in tool_definitions() if t["name"] == "query_metrics")

    assert metrics["input_schema"] == TOOLS["query_metrics"]["input_model"].model_json_schema()
    assert metrics["description"] == TOOLS["query_metrics"]["description"]


# -- what gets sent ---------------------------------------------------

def test_the_request_carries_the_prompt_the_tools_and_the_messages():
    client = FakeClient(_response())

    _call(client, system="be an SRE", messages=[{"role": "user", "content": "hi"}])

    sent = client.messages.kwargs
    assert sent["system"] == "be an SRE"
    assert sent["model"] == config.AGENT_MODEL
    assert sent["max_tokens"] == config.AGENT_MAX_TOKENS
    assert sent["messages"] == [{"role": "user", "content": "hi"}]
    assert len(sent["tools"]) == 4


def test_thinking_is_left_on():
    """With thinking off, the model can write a tool call into visible text,
    where it silently never runs."""
    client = FakeClient(_response())

    _call(client)

    assert client.messages.kwargs["thinking"]["type"] == "enabled"


def test_a_token_budget_is_sent_and_is_inside_the_api_bounds():
    """Sonnet 4.5 predates adaptive thinking: the budget is explicit, must be at
    least 1024, and must leave room under max_tokens for the answer itself."""
    client = FakeClient(_response())

    _call(client)

    budget = client.messages.kwargs["thinking"]["budget_tokens"]
    assert budget == config.AGENT_THINKING_BUDGET
    assert 1024 <= budget < config.AGENT_MAX_TOKENS


def test_no_output_config_is_sent_because_sonnet_4_5_rejects_effort():
    client = FakeClient(_response())

    _call(client)

    assert "output_config" not in client.messages.kwargs


# -- what comes back --------------------------------------------------

def test_content_blocks_come_back_as_plain_dicts():
    """The graph stores them in state and replays them; SDK objects would not
    survive a model_dump into the evidence column."""
    block = SimpleNamespace(to_dict=lambda: {"type": "text", "text": "hello"})
    client = FakeClient(_response(content=[block]))

    result = _call(client)

    assert result.content == [{"type": "text", "text": "hello"}]
    json.dumps(result.content)


def test_a_block_that_is_already_a_dict_is_passed_through():
    client = FakeClient(_response(content=[{"type": "tool_use", "id": "toolu_1"}]))

    assert _call(client).content[0]["id"] == "toolu_1"


def test_the_stop_reason_is_surfaced_for_the_caller_to_route_on():
    assert _call(FakeClient(_response())).stop_reason == "tool_use"


def test_a_refusal_is_reported_with_its_category():
    """Rare here, but it is a 200 with no tool calls - indistinguishable from
    "I am done" unless it is surfaced explicitly."""
    client = FakeClient(
        _response(
            stop_reason="refusal",
            stop_details=SimpleNamespace(category="cyber", explanation="no"),
            content=[],
        )
    )

    result = _call(client)

    assert result.stop_reason == "refusal"
    assert "cyber" in result.refusal


def test_a_successful_call_is_ok():
    assert _call(FakeClient(_response())).ok is True


# -- usage and cost ---------------------------------------------------

def test_usage_is_recorded_field_by_field():
    result = _call(FakeClient(_response()))

    assert result.input_tokens == 1000
    assert result.output_tokens == 500
    assert result.cache_read_tokens == 10000
    assert result.cache_creation_tokens == 2000


def test_a_usage_object_missing_the_cache_fields_reads_as_zero():
    client = FakeClient(_response(usage=SimpleNamespace(input_tokens=5, output_tokens=5)))

    result = _call(client)

    assert result.cache_read_tokens == 0
    assert result.cache_creation_tokens == 0


def test_the_cost_prices_cache_writes_and_reads_at_their_own_rates():
    """Opus 5 is $5/$25 per MTok; a cache write is 1.25x input and a read 0.1x.
    Charging cache reads at the full rate would overstate a six-turn run by
    roughly an order of magnitude."""
    cost = estimate_cost(
        "claude-opus-5",
        input_tokens=1000,
        output_tokens=500,
        cache_read_tokens=10000,
        cache_creation_tokens=2000,
    )

    expected = (1000 + 2000 * 1.25 + 10000 * 0.1) * 5 / 1e6 + 500 * 25 / 1e6
    assert cost == pytest.approx(expected)


def test_a_call_with_no_tokens_costs_nothing():
    assert estimate_cost("claude-opus-5", 0, 0, 0, 0) == 0.0


def test_the_bedrock_model_id_is_priced_rather_than_falling_through():
    """The fallback to Opus rates is silent: it does not error, it just
    overstates every call by ~1.7x and trips the budget stop early."""
    priced = estimate_cost(config.AGENT_MODEL, 1_000_000, 1_000_000, 0, 0)

    assert priced == pytest.approx(3.0 + 15.0)


def test_an_unpriced_model_still_reports_a_cost():
    """Reporting $0.00 for a mis-set AGENT_MODEL would silently defeat the
    budget stop condition."""
    assert estimate_cost("claude-something-new", 1000, 1000, 0, 0) > 0


def test_the_cost_reaches_the_result():
    assert _call(FakeClient(_response())).cost_usd > 0


def test_the_latency_is_measured():
    result = _call(FakeClient(_response()))

    assert isinstance(result.latency_ms, int)
    assert result.latency_ms >= 0


# -- failure ----------------------------------------------------------

def test_an_api_error_is_reported_rather_than_raised():
    client = FakeClient(error=RuntimeError("connection reset"))

    result = _call(client)

    assert result.ok is False
    assert "connection reset" in result.error
    assert "RuntimeError" in result.error


def test_a_failed_call_still_reports_zero_cost_and_no_content():
    result = _call(FakeClient(error=RuntimeError("boom")))

    assert result.content == []
    assert result.cost_usd == 0.0
    assert result.stop_reason is None


def test_a_failed_call_still_measures_its_latency():
    """A slow failure is the interesting kind, and it counts against the clock."""
    assert _call(FakeClient(error=RuntimeError("boom"))).latency_ms >= 0

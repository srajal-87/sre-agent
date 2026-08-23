"""Drift alarm on the tool registry. In the spirit of api/tests/test_models.py."""

import asyncio
import inspect
import json

import pytest

from agent.tools import TOOLS, get_tool, run_tool
from agent.tools.base import ToolResult

TOOL_NAMES = {"query_metrics", "query_logs", "query_deploy_history"}


def _exploding_sessionmaker():
    raise RuntimeError("no database in unit tests")


async def _run_offline(name, entry):
    """Invoke a runner in a way that cannot reach a backend."""
    if name == "query_metrics":
        return await entry["runner"](entry["input_model"](metric="no_such_metric"))
    if name == "query_logs":
        return await entry["runner"](entry["input_model"](service="no-such-service"))
    return await entry["runner"](
        entry["input_model"](), sessionmaker=_exploding_sessionmaker
    )


def test_the_registry_holds_exactly_the_three_read_only_tools():
    assert set(TOOLS) == TOOL_NAMES


@pytest.mark.parametrize("name", sorted(TOOL_NAMES))
def test_every_entry_has_the_four_required_keys(name):
    assert set(TOOLS[name]) == {"description", "input_model", "result_model", "runner"}


@pytest.mark.parametrize("name", sorted(TOOL_NAMES))
def test_every_description_is_written_for_a_model_not_a_programmer(name):
    description = TOOLS[name]["description"]
    assert description.strip()
    assert len(description) > 60  # a one-word label is not a tool description
    description.encode("ascii")


@pytest.mark.parametrize("name", sorted(TOOL_NAMES))
def test_every_result_model_subclasses_tool_result(name):
    assert issubclass(TOOLS[name]["result_model"], ToolResult)


@pytest.mark.parametrize("name", sorted(TOOL_NAMES))
def test_every_runner_is_an_async_callable(name):
    assert inspect.iscoroutinefunction(TOOLS[name]["runner"])


@pytest.mark.parametrize("name", sorted(TOOL_NAMES))
def test_the_tool_field_a_runner_returns_matches_its_registry_key(name):
    """A mismatch here silently mislabels every citation in the evidence trail."""
    entry = TOOLS[name]
    result = asyncio.run(_run_offline(name, entry))
    assert result.tool == name
    assert isinstance(result, entry["result_model"])


# ── the JSON schema is what the Anthropic tool-use API wants ─────────

@pytest.mark.parametrize("name", sorted(TOOL_NAMES))
def test_every_input_model_emits_a_usable_json_schema(name):
    schema = TOOLS[name]["input_model"].model_json_schema()

    assert schema["type"] == "object"
    assert schema["properties"]
    json.dumps(schema)  # must be serialisable as-is, with no adapter layer


def test_the_metrics_schema_advertises_the_known_metric_names():
    """The model must not have to guess a metric name."""
    schema = TOOLS["query_metrics"]["input_model"].model_json_schema()
    assert "http_requests_total" in json.dumps(schema)


# ── lookup helpers ───────────────────────────────────────────────────

def test_get_tool_returns_the_entry():
    assert get_tool("query_logs") is TOOLS["query_logs"]


def test_get_tool_names_the_alternatives_when_asked_for_an_unknown_tool():
    with pytest.raises(KeyError) as excinfo:
        get_tool("query_traces")
    assert "query_metrics" in str(excinfo.value)


def test_run_tool_validates_a_raw_dict_into_the_input_model():
    result = asyncio.run(run_tool("query_metrics", {"metric": "no_such_metric"}))
    assert result.tool == "query_metrics"
    assert result.ok is False


def test_run_tool_reports_a_malformed_argument_dict_instead_of_raising():
    """The model will send bad arguments; that must be an observation, not a crash."""
    result = asyncio.run(run_tool("query_metrics", {"lookback_minutes": "soon"}))

    assert result.ok is False
    assert "lookback_minutes" in result.error


def test_run_tool_reports_an_unknown_tool_instead_of_raising():
    result = asyncio.run(run_tool("query_traces", {}))
    assert result.ok is False
    assert "query_traces" in result.error
    assert isinstance(result, ToolResult)

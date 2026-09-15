"""Drift alarm on the action registry, and on its separation from TOOLS.

The write-side twin of test_registry.py. The assertions that matter most are at
the bottom: the model is handed TOOLS, and a write tool must never appear there.
"""

import asyncio
import inspect
import json

import pytest

from agent.graph.llm import tool_definitions
from agent.tools import TOOLS
from agent.tools.actions import (
    ACTIONS,
    ActionInput,
    ActionResult,
    get_action,
    run_action,
)

ACTION_NAMES = {"restart_service", "toggle_config", "rollback_deploy"}


def test_the_registry_holds_exactly_the_three_write_actions():
    assert set(ACTIONS) == ACTION_NAMES


@pytest.mark.parametrize("name", sorted(ACTION_NAMES))
def test_every_entry_has_the_four_required_keys(name):
    assert set(ACTIONS[name]) == {"description", "input_model", "result_model", "runner"}


@pytest.mark.parametrize("name", sorted(ACTION_NAMES))
def test_every_description_says_what_it_changes(name):
    description = ACTIONS[name]["description"]
    assert description.strip()
    assert len(description) > 60
    description.encode("ascii")


@pytest.mark.parametrize("name", sorted(ACTION_NAMES))
def test_every_input_model_names_a_target_service(name):
    model = ACTIONS[name]["input_model"]
    assert issubclass(model, ActionInput)
    assert "service" in model.model_json_schema()["required"]


@pytest.mark.parametrize("name", sorted(ACTION_NAMES))
def test_every_result_model_is_an_action_result(name):
    assert issubclass(ACTIONS[name]["result_model"], ActionResult)


@pytest.mark.parametrize("name", sorted(ACTION_NAMES))
def test_every_runner_is_an_async_callable(name):
    assert inspect.iscoroutinefunction(ACTIONS[name]["runner"])


@pytest.mark.parametrize("name", sorted(ACTION_NAMES))
def test_the_tool_field_an_action_returns_matches_its_registry_key(name):
    """A mismatch mislabels the action_taken column on every investigation."""
    result = asyncio.run(
        run_action(name, {"service": "data-service"}, allow_writes=False)
    )
    assert result.tool == name
    assert isinstance(result, ACTIONS[name]["result_model"])


@pytest.mark.parametrize("name", sorted(ACTION_NAMES))
def test_nothing_runs_with_writes_off(name):
    """The kill switch covers the whole registry, not the tools that remembered."""
    result = asyncio.run(
        run_action(name, {"service": "data-service"}, allow_writes=False)
    )
    assert result.dry_run is True
    assert result.executed is False
    assert result.ok is True


@pytest.mark.parametrize("name", sorted(ACTION_NAMES))
def test_every_input_model_emits_a_usable_json_schema(name):
    schema = ACTIONS[name]["input_model"].model_json_schema()

    assert schema["type"] == "object"
    assert schema["properties"]
    json.dumps(schema)


# ── lookup helpers ───────────────────────────────────────────────────

def test_get_action_returns_the_entry():
    assert get_action("restart_service") is ACTIONS["restart_service"]


def test_get_action_names_the_alternatives_for_an_unknown_action():
    with pytest.raises(KeyError) as excinfo:
        get_action("scale_up")
    assert "restart_service" in str(excinfo.value)


# ── the separation: the model is never handed a write tool ───────────

def test_no_action_appears_in_the_read_only_tool_registry():
    assert set(TOOLS) & ACTION_NAMES == set()


def test_the_model_is_offered_exactly_four_tools():
    """Three read tools plus update_hypothesis. An action here is a real bug."""
    names = {tool["name"] for tool in tool_definitions()}

    assert len(names) == 4
    assert names & ACTION_NAMES == set()


def test_no_tool_description_mentions_an_action():
    """The read tools must not hint at remediation; that is the gate's business."""
    for tool in tool_definitions():
        for name in ACTION_NAMES:
            assert name not in tool["description"]


def test_an_action_name_reaches_the_model_only_as_a_proposal_vocabulary():
    """update_hypothesis publishes the names so a proposal is not guesswork.
    Anywhere else - a callable tool, a read tool's schema - would be a real bug."""
    for tool in tool_definitions():
        body = json.dumps(tool["input_schema"])
        mentions = {name for name in ACTION_NAMES if name in body}
        if tool["name"] == "update_hypothesis":
            assert mentions == ACTION_NAMES
            assert json.dumps(
                tool["input_schema"]["properties"]["proposed_action"]["enum"]
            )
        else:
            assert mentions == set()

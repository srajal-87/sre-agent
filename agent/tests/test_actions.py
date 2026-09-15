"""ActionResult and run_action: the write-side envelope and the kill switch.

The registry is injected here so these tests describe the *machinery* and stay
true as the three real actions land beside it.
"""

import asyncio
import json

from agent.tools.actions import (
    ACTIONS_SOURCE,
    ActionInput,
    ActionResult,
    action_query,
    run_action,
)
from agent.tools.base import ToolResult


class _EchoInput(ActionInput):
    """Same shape as a real action's input: a service, and nothing surprising."""


class _EchoResult(ActionResult):
    pass


CALLS: list[str] = []


async def _echo(q: _EchoInput, **kwargs) -> _EchoResult:
    CALLS.append(q.service)
    return _EchoResult(
        tool="echo_service",
        summary=f"Echoed {q.service}.",
        source="test",
        query=action_query("echo_service", q),
        target=q.service,
        executed=True,
        verification="echoed",
    )


FAKE = {
    "echo_service": {
        "description": (
            "Echo the named service back. Stands in for a real write action in "
            "the registry machinery tests, and does nothing to the stack."
        ),
        "input_model": _EchoInput,
        "result_model": _EchoResult,
        "runner": _echo,
    }
}


def _run(name, arguments, **kwargs) -> ActionResult:
    CALLS.clear()
    return asyncio.run(run_action(name, arguments, registry=FAKE, **kwargs))


# ── the envelope ─────────────────────────────────────────────────────

def test_an_action_result_is_a_tool_result():
    """source/query/error/latency_ms and the never-raises contract carry over."""
    assert issubclass(ActionResult, ToolResult)


def test_an_action_result_claims_nothing_by_default():
    result = ActionResult(tool="t", summary="s", source="src", query="q")

    assert result.executed is False
    assert result.dry_run is False
    assert result.target == ""
    assert result.verification is None


def test_every_action_input_names_a_target_service():
    assert "service" in _EchoInput.model_json_schema()["required"]


def test_the_query_is_the_literal_call_that_was_issued():
    assert action_query("echo_service", _EchoInput(service="data-service")) == (
        "echo_service(service=data-service)"
    )


# ── writes on ────────────────────────────────────────────────────────

def test_with_writes_on_the_runner_actually_runs():
    result = _run("echo_service", {"service": "data-service"}, allow_writes=True)

    assert CALLS == ["data-service"]
    assert result.ok is True
    assert result.executed is True
    assert result.dry_run is False
    assert result.target == "data-service"


# ── writes off: the kill switch, in one place ────────────────────────

def test_with_writes_off_nothing_runs():
    result = _run("echo_service", {"service": "data-service"}, allow_writes=False)

    assert CALLS == []
    assert result.executed is False


def test_a_dry_run_is_a_success_that_says_what_would_have_happened():
    """Not a failure: the gate ran and its decision is still worth recording."""
    result = _run("echo_service", {"service": "data-service"}, allow_writes=False)

    assert result.ok is True
    assert result.dry_run is True
    assert result.error is None
    assert "echo_service(service=data-service)" in result.summary
    assert "AGENT_ALLOW_WRITES" in result.summary


def test_a_dry_run_still_returns_the_actions_own_result_model():
    result = _run("echo_service", {"service": "data-service"}, allow_writes=False)

    assert isinstance(result, _EchoResult)
    assert result.tool == "echo_service"
    assert result.target == "data-service"


def test_the_kill_switch_defaults_to_the_config_flag():
    """AGENT_ALLOW_WRITES is off in the test environment, so this is a dry run."""
    result = _run("echo_service", {"service": "data-service"})

    assert result.dry_run is True
    assert CALLS == []


# ── expected failures never raise ────────────────────────────────────

def test_an_unknown_action_is_reported_not_raised():
    result = _run("delete_database", {"service": "data-service"})

    assert result.ok is False
    assert "delete_database" in result.error
    assert "echo_service" in result.error
    assert isinstance(result, ActionResult)
    assert result.source == ACTIONS_SOURCE


def test_a_hallucinated_action_never_counts_as_executed():
    result = _run("delete_database", {"service": "data-service"})

    assert result.executed is False
    assert result.dry_run is False


def test_malformed_arguments_are_reported_not_raised():
    result = _run("echo_service", {}, allow_writes=True)

    assert result.ok is False
    assert "service" in result.error
    assert result.executed is False
    assert CALLS == []


def test_arguments_are_validated_before_the_kill_switch_is_consulted():
    """A dry run of a call that could never have run would be a lie."""
    result = _run("echo_service", {}, allow_writes=False)

    assert result.ok is False
    assert result.dry_run is False


# ── the write side is storable and console-safe ──────────────────────

def test_the_result_survives_a_strict_json_dump_for_the_action_result_column():
    result = _run("echo_service", {"service": "data-service"}, allow_writes=True)
    json.dumps(result.model_dump(mode="json"), allow_nan=False)


def test_action_facing_strings_are_ascii():
    result = _run("echo_service", {"service": "data-service"})
    result.summary.encode("ascii")

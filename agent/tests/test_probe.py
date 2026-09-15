"""The probe CLI: one tool, one JSON document on stdout."""

import json

from agent.tools import probe


def test_prints_a_json_document_for_one_tool(capsys):
    exit_code = probe.main(["--tool", "query_metrics", "--args", '{"metric": "nope"}'])
    payload = json.loads(capsys.readouterr().out)

    assert payload["tool"] == "query_metrics"
    assert payload["ok"] is False
    assert exit_code == 1  # a failed observation is a non-zero exit


def test_an_unknown_service_is_a_failed_observation(capsys):
    exit_code = probe.main(["--tool", "query_logs", "--args", '{"service": "nope"}'])
    capsys.readouterr()
    assert exit_code == 1


def test_args_default_to_an_empty_object(capsys):
    probe.main(["--tool", "query_metrics"])
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is False  # metric is required


def test_malformed_args_json_is_reported_not_raised(capsys):
    exit_code = probe.main(["--tool", "query_metrics", "--args", "{not json"])
    out = capsys.readouterr().out

    assert exit_code == 2
    assert "json" in out.lower()


def test_unknown_tool_lists_the_available_ones(capsys):
    exit_code = probe.main(["--tool", "query_traces", "--args", "{}"])
    out = capsys.readouterr().out

    assert exit_code == 1
    assert "query_metrics" in out


def test_list_prints_every_tool_with_its_description(capsys):
    exit_code = probe.main(["--list"])
    out = capsys.readouterr().out

    assert exit_code == 0
    for name in ("query_metrics", "query_logs", "query_deploy_history"):
        assert name in out


def test_the_printed_document_is_the_full_tool_result(capsys):
    """Whatever the agent would see, the operator sees too."""
    probe.main(["--tool", "query_metrics", "--args", '{"metric": "nope"}'])
    payload = json.loads(capsys.readouterr().out)

    for key in ("tool", "ok", "summary", "source", "query", "notes", "error", "latency_ms"):
        assert key in payload


# ── the write side ───────────────────────────────────────────────────

def test_list_actions_prints_every_action_with_its_description(capsys):
    exit_code = probe.main(["--list-actions"])
    out = capsys.readouterr().out

    assert exit_code == 0
    for name in ("restart_service", "toggle_config", "rollback_deploy"):
        assert name in out


def test_an_action_is_a_dry_run_unless_execute_is_passed(capsys):
    """The default must never touch the stack, whatever the environment says."""
    exit_code = probe.main(
        ["--action", "restart_service", "--args", '{"service": "downstream-dep"}']
    )
    payload = json.loads(capsys.readouterr().out)

    assert exit_code == 0
    assert payload["dry_run"] is True
    assert payload["executed"] is False
    assert "restart_service(service=downstream-dep)" in payload["summary"]


def test_execute_turns_the_write_on_for_this_one_call(capsys):
    """An unknown service, so the action refuses before reaching Docker."""
    exit_code = probe.main(
        ["--action", "restart_service", "--args", '{"service": "nope"}', "--execute"]
    )
    payload = json.loads(capsys.readouterr().out)

    assert exit_code == 1
    assert payload["dry_run"] is False
    assert payload["executed"] is False
    assert "nope" in payload["error"]


def test_an_unknown_action_lists_the_available_ones(capsys):
    exit_code = probe.main(["--action", "scale_up", "--args", '{"service": "x"}'])
    out = capsys.readouterr().out

    assert exit_code == 1
    assert "restart_service" in out


def test_malformed_action_args_are_reported_not_raised(capsys):
    exit_code = probe.main(["--action", "restart_service", "--args", "{not json"])
    out = capsys.readouterr().out

    assert exit_code == 2
    assert "json" in out.lower()


def test_a_tool_and_an_action_at_once_is_a_usage_error(capsys):
    exit_code = probe.main(["--tool", "query_metrics", "--action", "restart_service"])
    out = capsys.readouterr().out

    assert exit_code == 2
    assert "--action" in out


def test_naming_neither_is_a_usage_error(capsys):
    exit_code = probe.main([])
    out = capsys.readouterr().out

    assert exit_code == 2
    assert "--tool" in out and "--action" in out

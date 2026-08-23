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

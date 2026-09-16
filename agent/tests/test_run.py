"""The CLI runner: one investigation, printed.

Sibling of agent/tools/probe.py, and there for the same reason - so the graph
can be exercised by hand against the live stack during a real injected fault,
before anything else depends on it.

The collaborators are injected here too, so these tests run the whole CLI with
no API key and no backends.
"""

import json
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import pytest

from agent import config
from agent.graph.llm import ModelResponse
from agent.graph.render import summarise_alert
from agent.graph.run import main, render_report
from agent.graph.state import InvestigationReport
from agent.tools.base import ActionResult, ToolResult

SCENARIOS = Path(__file__).resolve().parents[2] / "eval" / "scenarios"

T0 = datetime(2026, 8, 25, 12, 0, tzinfo=timezone.utc)

QUERY = "promql for the gateway"


async def _runner(name, arguments, **kwargs):
    return ToolResult(
        tool=name, summary=f"summary from {name}", source="fake", query=QUERY
    )


class Model:
    """One confident turn, cited, then done."""

    def __init__(self, ok=True):
        self._ok = ok

    async def __call__(self, **kwargs):
        if not self._ok:
            return ModelResponse(ok=False, error="APIConnectionError: no route")
        return ModelResponse(
            content=[
                {
                    "type": "tool_use",
                    "id": "toolu_h1",
                    "name": "update_hypothesis",
                    "input": {
                        "fault_type": "timeout",
                        "service": "api-gateway",
                        "statement": "the gateway is timing out on its own calls",
                        "confidence": 0.91,
                        "rationale": "The gateway's counter rose while downstream stayed flat.",
                        "citations": [QUERY],
                        "ruled_out": ["code change - no deploys in the window"],
                    },
                }
            ],
            stop_reason="tool_use",
            cost_usd=0.0413,
        )


def _main(argv, model=None, capsys=None):
    code = main(argv, call=model or Model(), run=_runner)
    return code, capsys.readouterr().out


@pytest.fixture
def alert_file(tmp_path):
    payload = {
        "status": "firing",
        "commonLabels": {
            "alertname": "GatewayTimeouts",
            "service": "api-gateway",
            "severity": "critical",
        },
        "commonAnnotations": {"summary": "api-gateway is returning 504s"},
        "alerts": [{"status": "firing", "labels": {}, "annotations": {}}],
    }
    path = tmp_path / "alert.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


# -- the happy path ---------------------------------------------------

def test_a_completed_investigation_exits_zero(alert_file, capsys):
    code, _ = _main(["--alert", str(alert_file)], capsys=capsys)

    assert code == 0


def test_the_summary_leads_with_the_diagnosis(alert_file, capsys):
    _, out = _main(["--alert", str(alert_file)], capsys=capsys)

    assert "timing out on its own calls" in out
    assert "timeout" in out
    assert "0.91" in out


def test_the_summary_shows_why_the_run_ended(alert_file, capsys):
    _, out = _main(["--alert", str(alert_file)], capsys=capsys)

    assert "confident" in out


def test_the_summary_shows_the_citations_so_they_can_be_checked_by_hand(
    alert_file, capsys
):
    """The point of the rehearsal is verifying each citation against the tool
    output, so they have to be on screen."""
    _, out = _main(["--alert", str(alert_file)], capsys=capsys)

    assert QUERY in out


def test_the_summary_shows_what_was_ruled_out(alert_file, capsys):
    _, out = _main(["--alert", str(alert_file)], capsys=capsys)

    assert "no deploys in the window" in out


def test_the_summary_shows_the_bill(alert_file, capsys):
    _, out = _main(["--alert", str(alert_file)], capsys=capsys)

    assert "0.0413" in out


def test_the_output_is_ascii(alert_file, capsys):
    """It gets printed to a Windows console."""
    _, out = _main(["--alert", str(alert_file)], capsys=capsys)

    out.encode("ascii")


def test_the_json_flag_prints_the_whole_report(alert_file, capsys):
    _, out = _main(["--alert", str(alert_file), "--json"], capsys=capsys)
    report = json.loads(out)

    assert report["fault_type"] == "timeout"
    assert report["evidence"]["results"]


def test_a_reference_time_can_be_forced(alert_file, capsys):
    """So a fault window can be investigated after the fact."""
    _, out = _main(
        ["--alert", str(alert_file), "--reference-time", "2026-08-25T12:00:00Z"],
        capsys=capsys,
    )

    assert "2026-08-25T12:00:00" in out


# -- the audit trail --------------------------------------------------

def test_a_traced_run_prints_the_id_and_the_project_to_look_it_up_in():
    """No URL: a LangSmith deep link needs an organisation id this process has
    no way to know, and a broken link is worse than an id to paste into the run
    list's filter."""
    trace_id = uuid4()
    report = InvestigationReport(diagnosis="d", trace_id=trace_id)

    out = render_report(report, T0)

    assert str(trace_id) in out
    assert config.LANGSMITH_PROJECT in out


def test_an_untraced_run_says_nothing_about_a_trace():
    """An empty "trace:" line would read as a trace that failed to record."""
    out = render_report(InvestigationReport(diagnosis="d"), T0)

    assert "trace" not in out.lower()


def test_the_span_queue_is_drained_before_the_process_exits(alert_file, capsys):
    """Spans are posted from a background thread, so a CLI that returns as soon
    as the report is printed loses the trace it just paid for."""
    drained = []
    code = main(
        ["--alert", str(alert_file)],
        call=Model(), run=_runner, flush=lambda: drained.append(True),
    )
    capsys.readouterr()

    assert code == 0
    assert drained == [True]


def test_the_queue_is_drained_even_when_the_investigation_failed(
    alert_file, capsys
):
    """That run is exactly the one whose trace is worth reading."""
    drained = []
    main(
        ["--alert", str(alert_file)],
        call=Model(ok=False), run=_runner, flush=lambda: drained.append(True),
    )
    capsys.readouterr()

    assert drained == [True]


# -- failure ----------------------------------------------------------

def test_a_failed_investigation_exits_one(alert_file, capsys):
    code, out = _main(["--alert", str(alert_file)], Model(ok=False), capsys)

    assert code == 1
    assert "no route" in out


def test_a_missing_alert_argument_is_a_usage_error(capsys):
    code, out = _main([], capsys=capsys)

    assert code == 2
    assert "--alert" in out


def test_a_file_that_is_not_there_is_reported_not_raised(capsys, tmp_path):
    code, out = _main(["--alert", str(tmp_path / "nope.json")], capsys=capsys)

    assert code == 2
    assert "nope.json" in out


def test_a_file_that_is_not_json_is_reported(capsys, tmp_path):
    path = tmp_path / "bad.json"
    path.write_text("{not json", encoding="utf-8")

    code, out = _main(["--alert", str(path)], capsys=capsys)

    assert code == 2
    assert "json" in out.lower()


def test_a_payload_with_no_alerts_is_reported(capsys, tmp_path):
    path = tmp_path / "empty.json"
    path.write_text(json.dumps({"status": "firing", "alerts": []}), encoding="utf-8")

    code, out = _main(["--alert", str(path)], capsys=capsys)

    assert code == 2
    assert "alert" in out.lower()


def test_a_bad_reference_time_is_reported(alert_file, capsys):
    code, out = _main(
        ["--alert", str(alert_file), "--reference-time", "yesterday"], capsys=capsys
    )

    assert code == 2
    assert "reference-time" in out


# -- the checked-in scenarios -----------------------------------------

def _scenario_files():
    return sorted(SCENARIOS.glob("*.json"))


def test_there_is_a_scenario_for_every_fault_the_injector_can_produce():
    names = {path.stem for path in _scenario_files()}

    assert names == {
        "gateway-timeout",
        "downstream-latency",
        "data-service-bad-config",
        "downstream-memory",
    }


@pytest.mark.parametrize("path", _scenario_files(), ids=lambda p: p.stem)
def test_every_scenario_parses_into_an_alert_summary(path):
    alert = summarise_alert(json.loads(path.read_text(encoding="utf-8")))

    assert alert.alertname
    assert alert.service in {"api-gateway", "data-service", "downstream-dep"}
    assert alert.summary


@pytest.mark.parametrize("path", _scenario_files(), ids=lambda p: p.stem)
def test_no_scenario_hard_codes_a_start_time(path):
    """A checked-in timestamp would anchor every window to a date in the past,
    and the run would quietly read an empty window."""
    payload = json.loads(path.read_text(encoding="utf-8"))

    assert all(not alert.get("startsAt") for alert in payload["alerts"])


@pytest.mark.parametrize("path", _scenario_files(), ids=lambda p: p.stem)
def test_no_scenario_names_the_fault_it_is_hiding(path):
    """The scenario is the alert an operator would see, not the answer."""
    text = json.loads(path.read_text(encoding="utf-8"))
    dumped = json.dumps(text).lower()

    for label in ("bad_config", "resource_exhaustion", "ground_truth"):
        assert label not in dumped


def test_a_scenario_file_runs_end_to_end(capsys):
    code, out = _main(
        ["--alert", str(SCENARIOS / "gateway-timeout.json")], capsys=capsys
    )

    assert code == 0
    assert "timeout" in out


# -- the policy gate and the write flag -------------------------------

class Remediable:
    """One confident, cited turn that proposes a low-radius action."""

    async def __call__(self, **kwargs):
        return ModelResponse(
            content=[
                {
                    "type": "tool_use",
                    "id": "toolu_h1",
                    "name": "update_hypothesis",
                    "input": {
                        "fault_type": "bad_config",
                        "service": "data-service",
                        "statement": "data-service is serving a corrupted target",
                        "confidence": 0.95,
                        "rationale": "config_errors_total climbs from the alert on.",
                        "citations": [QUERY],
                        "proposed_action": "toggle_config",
                        "action_target": "data-service",
                    },
                }
            ],
            stop_reason="tool_use",
            cost_usd=0.01,
        )


class Actor:
    """Stands in for run_action, and records the flag it saw."""

    def __init__(self):
        self.calls = []
        self.writes_allowed = []

    async def __call__(self, name, arguments, **kwargs):
        self.calls.append((name, arguments))
        self.writes_allowed.append(config.AGENT_ALLOW_WRITES)
        return ActionResult(
            tool=name, summary=f"ran {name}", source="fake",
            query=f"{name}(service={arguments['service']})",
            target=arguments["service"], executed=True,
            verification="health returned 200",
        )


def _remediable(argv, capsys, actor=None):
    actor = actor or Actor()
    code = main(argv, call=Remediable(), run=_runner, act_on=actor)
    return code, capsys.readouterr().out, actor


def test_the_summary_names_the_action_and_its_blast_radius(alert_file, capsys):
    _, out, _ = _remediable(["--alert", str(alert_file)], capsys)

    assert "toggle_config" in out
    assert "data-service" in out
    assert "low" in out


def test_the_summary_gives_the_gates_reason(alert_file, capsys):
    """A denial that does not say why is not an audit trail."""
    _, out = _main(["--alert", str(alert_file)], capsys=capsys)

    assert "escalate" in out
    assert "no_action_proposed" in out


def test_the_summary_shows_what_the_action_reported(alert_file, capsys):
    _, out, _ = _remediable(["--alert", str(alert_file)], capsys)

    assert "ran toggle_config" in out


def test_writes_are_off_unless_the_flag_is_passed(alert_file, capsys):
    _, _, actor = _remediable(["--alert", str(alert_file)], capsys)

    assert actor.writes_allowed == [False]


def test_allow_writes_turns_them_on_for_this_run(alert_file, capsys):
    _, _, actor = _remediable(["--alert", str(alert_file), "--allow-writes"], capsys)

    assert actor.writes_allowed == [True]


def test_the_flag_does_not_outlive_the_run(alert_file, capsys):
    """One run, not the rest of the process."""
    _remediable(["--alert", str(alert_file), "--allow-writes"], capsys)

    assert config.AGENT_ALLOW_WRITES is False


def test_the_gate_output_is_ascii(alert_file, capsys):
    _, out, _ = _remediable(["--alert", str(alert_file)], capsys)

    out.encode("ascii")

"""The CLI runner: one investigation, printed.

Sibling of agent/tools/probe.py, and there for the same reason - so the graph
can be exercised by hand against the live stack during a real injected fault,
before anything else depends on it.

The collaborators are injected here too, so these tests run the whole CLI with
no API key and no backends.
"""

import json
from pathlib import Path

import pytest

from agent.graph.llm import ModelResponse
from agent.graph.render import summarise_alert
from agent.graph.run import main
from agent.tools.base import ToolResult

SCENARIOS = Path(__file__).resolve().parents[2] / "eval" / "scenarios"

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

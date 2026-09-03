"""Run one investigation by hand and print what the agent concluded.

Sibling of ``agent/tools/probe.py``, and there for the same reason: the graph is
exercised against the live stack during a real injected fault before anything
else depends on it. The citations are printed in full because verifying each one
against the tool output *is* the rehearsal.

Usage:
    python -m agent.graph.run --alert eval/scenarios/gateway-timeout.json
    python -m agent.graph.run --alert eval/scenarios/downstream-memory.json --json
    python -m agent.graph.run --alert <file> --reference-time 2026-08-25T12:00:00Z

Exit codes: 0 = completed, 1 = the investigation failed, 2 = bad usage.
"""

import argparse
import asyncio
import json
from datetime import datetime, timezone

from agent.graph import investigate
from agent.graph.llm import call_model
from agent.graph.render import summarise_alert
from agent.graph.state import InvestigationReport
from agent.tools import run_tool


def _fail(message: str) -> int:
    print(f"error: {message}")
    return 2


def _load_alert(path: str):
    """Read an Alertmanager payload from disk. Reports rather than raises."""
    try:
        with open(path, encoding="utf-8") as handle:
            payload = json.load(handle)
    except OSError as exc:
        raise ValueError(f"could not read {path}: {exc}") from None
    except json.JSONDecodeError as exc:
        raise ValueError(f"{path} is not valid json: {exc}") from None
    return summarise_alert(payload)


def _bullets(title: str, values: list[str]) -> list[str]:
    if not values:
        return []
    return [f"{title}:"] + [f"  - {value}" for value in values]


def render_report(report: InvestigationReport, reference_time: datetime) -> str:
    """The human-readable summary. ASCII - this reaches a Windows console."""
    lines = [
        "",
        f"INVESTIGATION  {report.fault_type} in {report.service or 'an unknown service'}",
        f"reference time: {reference_time.isoformat()}",
        "",
        f"diagnosis      : {report.diagnosis}",
        f"confidence     : {report.confidence}",
        f"recommendation : {report.recommendation}",
        f"stopped because: {report.stop_reason}",
        f"status         : {report.status}",
        "",
    ]
    lines += _bullets("citations", report.citations)
    lines += _bullets("ruled out", report.ruled_out)
    lines += _bullets("notes", report.notes)
    if report.error:
        lines += ["", f"errors: {report.error}"]

    tool_calls = len(report.evidence.get("results", []))
    lines += [
        "",
        f"{report.steps} step(s) | {report.llm_calls} model call(s) | "
        f"{tool_calls} tool call(s) | ${report.cost_usd} | "
        f"{report.latency_ms / 1000:.1f}s",
        "",
    ]
    return "\n".join(lines)


def main(argv: list[str] | None = None, *, call=call_model, run=run_tool) -> int:
    parser = argparse.ArgumentParser(
        description="Investigate one alert and print the report."
    )
    parser.add_argument("--alert", help="path to an Alertmanager webhook payload")
    parser.add_argument(
        "--reference-time",
        help="ISO timestamp to anchor the investigation to; defaults to the "
        "alert's startsAt, or now",
    )
    parser.add_argument(
        "--json", action="store_true", help="print the whole report as json"
    )
    args = parser.parse_args(argv)

    if not args.alert:
        return _fail("--alert is required (path to an Alertmanager payload)")

    try:
        alert = _load_alert(args.alert)
    except ValueError as exc:
        return _fail(str(exc))

    if args.reference_time:
        try:
            alert.started_at = datetime.fromisoformat(
                args.reference_time.replace("Z", "+00:00")
            )
        except ValueError:
            return _fail(
                f"--reference-time '{args.reference_time}' is not an ISO timestamp"
            )
        if alert.started_at.tzinfo is None:
            alert.started_at = alert.started_at.replace(tzinfo=timezone.utc)

    report = asyncio.run(investigate(alert, call=call, run=run))

    if args.json:
        print(json.dumps(report.model_dump(mode="json"), indent=2, default=str))
    else:
        reference = alert.started_at or datetime.now(timezone.utc)
        print(render_report(report, reference))

    return 0 if report.status == "completed" else 1


if __name__ == "__main__":
    raise SystemExit(main())

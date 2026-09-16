"""Run one investigation by hand and print what the agent concluded.

Sibling of ``agent/tools/probe.py``, and there for the same reason: the graph is
exercised against the live stack during a real injected fault before anything
else depends on it. The citations are printed in full because verifying each one
against the tool output *is* the rehearsal.

Usage:
    python -m agent.graph.run --alert eval/scenarios/gateway-timeout.json
    python -m agent.graph.run --alert eval/scenarios/downstream-memory.json --json
    python -m agent.graph.run --alert <file> --reference-time 2026-08-25T12:00:00Z
    python -m agent.graph.run --alert <file> --allow-writes

Without --allow-writes an approved action is a dry run: the policy gate still
reaches its verdict and prints it, and nothing in the stack is touched. That is
the useful default for a rehearsal - the gate's decision is what is being
checked, not its effect.

Exit codes: 0 = completed, 1 = the investigation failed, 2 = bad usage.
"""

import argparse
import asyncio
import json
from datetime import datetime, timezone

from agent import config
from agent.graph import investigate, trace
from agent.graph.llm import call_model
from agent.graph.render import summarise_alert
from agent.graph.state import InvestigationReport
from agent.tools import run_tool
from agent.tools.actions import run_action


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
    ]
    if report.trace_id:
        # The id, not a URL: a LangSmith deep link needs an organisation id this
        # process has no way to know, and a broken link is worse than an id to
        # paste into the run list's filter.
        lines.append(
            f"trace          : {report.trace_id} "
            f"(project '{config.LANGSMITH_PROJECT}')"
        )
    lines.append("")
    decision = report.policy_decision
    if decision is not None:
        lines += [
            "POLICY",
            f"  proposed  : {decision.action or 'nothing'}"
            + (f" on {decision.target}" if decision.target else ""),
            f"  radius    : {decision.blast_radius or 'n/a'}",
            f"  verdict   : {'approved' if decision.approved else 'denied'}"
            + (f" ({decision.rule})" if decision.rule else ""),
            f"  because   : {decision.reason}",
        ]
        if report.action_result:
            lines += [
                f"  action    : {report.action_taken or 'nothing was done'}",
                f"  outcome   : {report.action_result}",
            ]
        lines += [""]

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


def main(
    argv: list[str] | None = None,
    *,
    call=call_model,
    run=run_tool,
    act_on=run_action,
    flush=trace.flush,
) -> int:
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
    parser.add_argument(
        "--allow-writes",
        action="store_true",
        help="let an approved action actually run (default: dry run)",
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

    # run_action reads this constant at call time, so this is the override
    # point. Restored afterwards: the flag is for one run, not for the rest of
    # the process - which also keeps the in-process CLI tests honest.
    previous_writes = config.AGENT_ALLOW_WRITES
    if args.allow_writes:
        config.AGENT_ALLOW_WRITES = True
    try:
        report = asyncio.run(investigate(alert, call=call, run=run, act_on=act_on))
    finally:
        config.AGENT_ALLOW_WRITES = previous_writes
        # Spans are posted from a background thread, so a process that returns
        # as soon as the report is printed exits with the queue still full and
        # loses the trace it just paid for. A no-op when nothing was traced,
        # and it never raises - including on the run that failed, which is
        # exactly the trace worth reading.
        flush()

    if args.json:
        print(json.dumps(report.model_dump(mode="json"), indent=2, default=str))
    else:
        reference = alert.started_at or datetime.now(timezone.utc)
        print(render_report(report, reference))

    return 0 if report.status == "completed" else 1


if __name__ == "__main__":
    raise SystemExit(main())

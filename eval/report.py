"""Render a Summary as markdown, for README.md and for the console.

ASCII only - this gets printed to a Windows console, the same constraint the
tool layer works under.

Every rate prints its numerator and denominator. With four scenarios and small
repeat counts, "25%" is 1/4 dressed up as a measurement, and a reader cannot
tell 0/4 from "no data" once both are percentages.
"""

from eval.score import Rate, Summary


def _rate(rate: Rate) -> str:
    """`x/N (p%)`, or a dash when there was nothing to count."""
    if rate.denominator == 0:
        return "-"
    return f"{rate.numerator}/{rate.denominator} ({rate.value * 100:.0f}%)"


def _money(value: float | None) -> str:
    return "-" if value is None else f"${value:.3f}"


def _seconds(ms: float | None) -> str:
    return "-" if ms is None else f"{ms / 1000:.0f}s"


def _header(summary: Summary) -> str:
    """The line a reader remembers: what the suite cost and how long it took."""
    parts = [f"${summary.total_cost_usd:.2f} for {summary.runs_total} investigations"]
    if summary.wall_clock_seconds is not None:
        parts.append(f"{summary.wall_clock_seconds / 60:.0f} minutes wall clock")
    if summary.runs_void:
        parts.append(f"{summary.runs_void} excluded as void")
    if summary.failed_runs:
        parts.append(f"{summary.failed_runs} failed")
    parts.append(f"{summary.executed_runs} of {summary.runs_total} runs executed an action")
    return "Suite: " + "; ".join(parts) + "."


def render(summary: Summary) -> str:
    """The suite as a markdown document. One public function, by design."""
    lines = [
        _header(summary),
        "",
        "| Metric | Value |",
        "|--------|-------|",
        f"| Diagnosis accuracy | {_rate(summary.diagnosis_accuracy)} |",
        f"| False autonomous action rate | {_rate(summary.false_autonomous_action)} |",
        f"| Avg cost per investigation | {_money(summary.cost_usd.mean)} |",
        f"| Avg latency per investigation | {_seconds(summary.latency_ms.mean)} |",
        "",
        "### By scenario",
        "",
        "| Scenario | Runs | Void | Diagnosis | Fault type | Service | Approved |",
        "|----------|------|------|-----------|------------|---------|----------|",
    ]
    for name, row in summary.by_scenario.items():
        expected = f"{row.runs_total}/{row.runs_expected}" if row.runs_expected else str(row.runs_total)
        lines.append(
            f"| {name} | {expected} | {row.runs_void} | "
            f"{_rate(row.diagnosis_accuracy)} | {_rate(row.fault_type_accuracy)} | "
            f"{_rate(row.service_accuracy)} | {_rate(row.autonomous_action)} |"
        )

    lines += [
        "",
        "### Missed remediations, by the rule that denied them",
        "",
        "| Rule | Count |",
        "|------|-------|",
    ]
    lines += [
        f"| {rule} | {count} |"
        for rule, count in summary.missed_remediation_by_rule.items()
    ]

    lines += [
        "",
        "Notes:",
        "- Every rate is printed as x/N. Percentages over a handful of runs are",
        "  false precision, and a dash means no data rather than zero.",
        "- Cost and latency cover completed runs only: a run that died on its",
        "  first turn has a tiny latency that would flatter the mean.",
        "- The false-autonomous-action rate could only ever fire on three of the",
        "  four scenarios. No low-blast-radius action addresses a timeout fault,",
        "  so the policy gate cannot approve anything on a timeout run - the one",
        "  scenario the agent is known to misdiagnose.",
    ]
    return "\n".join(lines)

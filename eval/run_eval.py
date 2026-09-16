"""Run the evaluation suite against the live stack, and score what comes back.

    python eval/run_eval.py                          # every scenario, 3 repeats
    python eval/run_eval.py --scenario gateway-timeout --repeat 5
    python eval/run_eval.py --dry-run                # plan, wall clock, cost
    python eval/run_eval.py --replay eval/results/<suite>.jsonl

Sibling of `agent/graph/run.py` and `agent/tools/probe.py`, and its exit codes
match: 0 = completed, 1 = a run failed or was void, 2 = bad usage.

`--dry-run` is not a nicety. A full suite is over an hour of wall clock and real
money, and an accidental one is worth a flag to prevent.
"""

import argparse
import asyncio
import sys
from pathlib import Path

if __package__ in (None, ""):  # `python eval/run_eval.py`, the documented form
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from eval.harness import (
    CYCLE_SECONDS,
    DEFAULT_RESULTS_DIR,
    OBSERVED_COST_PER_RUN_USD,
    SCENARIOS,
    append_run,
    load_results,
    run_suite,
)
from eval.report import render
from eval.score import RunRecord, aggregate, score_run
from eval.truth import GROUND_TRUTH_PATH, find_by_incident_id, load_ground_truth


def _fail(message: str) -> int:
    print(f"error: {message}")
    return 2


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the SRE agent evaluation suite.")
    parser.add_argument(
        "--scenario",
        action="append",
        help="run only this scenario (repeatable); default is all of them",
    )
    parser.add_argument(
        "--repeat", type=int, default=3, help="runs per scenario (default 3)"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="print the plan and its estimated cost; run nothing",
    )
    parser.add_argument(
        "--results-dir",
        default=str(DEFAULT_RESULTS_DIR),
        help="where the suite's results JSONL is written",
    )
    parser.add_argument(
        "--replay",
        help="score an existing results JSONL instead of running anything",
    )
    parser.add_argument(
        "--ground-truth",
        default=str(GROUND_TRUTH_PATH),
        help="the injector's ground truth JSONL (default: injector/ground_truth.jsonl)",
    )
    parser.add_argument(
        "--out",
        help="also write the rendered markdown here, for README.md",
    )
    parser.add_argument(
        "--allow-writes",
        action="store_true",
        help="let an approved action actually run (default: dry)",
    )
    return parser


def main(argv: list[str] | None = None, *, suite=run_suite) -> int:
    args = _parser().parse_args(argv)

    if args.replay:
        try:
            records = load_results(Path(args.replay))
            truth_rows = load_ground_truth(args.ground_truth)
        except OSError as exc:
            return _fail(str(exc))
        return _score_and_print(records, truth_rows, out=args.out)

    names = args.scenario or list(SCENARIOS)
    unknown = [name for name in names if name not in SCENARIOS]
    if unknown:
        return _fail(
            f"unknown scenario(s): {', '.join(unknown)}; known: {', '.join(SCENARIOS)}"
        )
    if args.repeat < 1:
        return _fail(f"--repeat must be at least 1, got {args.repeat}")

    scenarios = [SCENARIOS[name] for name in names]

    if args.dry_run:
        print(_plan(scenarios, args.repeat, allow_writes=args.allow_writes))
        return 0

    results_path = Path(args.results_dir) / _suite_filename()
    print(_plan(scenarios, args.repeat, allow_writes=args.allow_writes))
    print(f"\nresults: {results_path}\n")

    records = asyncio.run(
        suite(
            scenarios,
            repeat=args.repeat,
            allow_writes=args.allow_writes,
            persist=append_run(results_path),
        )
    )

    try:
        truth_rows = load_ground_truth(args.ground_truth)
    except OSError:
        # The injector writes this file; a missing one means no run recorded
        # ground truth, which the scorer will void rather than guess at.
        truth_rows = []
    # The same scoring path replay uses, so live and replay cannot diverge.
    return _score_and_print(records, truth_rows, out=args.out)


def _suite_filename() -> str:
    """One file per suite, named for when it started."""
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).strftime("suite-%Y%m%dT%H%M%SZ.jsonl")


def _score_and_print(records: list[RunRecord], truth_rows, *, out: str | None = None) -> int:
    """Join each run to its truth row, score, render. No I/O beyond printing.

    The join is by incident id, and a run whose truth row never landed is left
    void rather than scored as wrong - there is nothing to compare it against.
    """
    scored = []
    for record in records:
        if record.truth is None and record.incident_id:
            record = record.model_copy(
                update={"truth": find_by_incident_id(truth_rows, record.incident_id)}
            )
        scored.append(score_run(record))

    summary = aggregate(scored)
    markdown = render(summary)
    print(markdown)
    if out:
        destination = Path(out)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(markdown + "\n", encoding="utf-8")
        print(f"\nwritten to {destination}")
    # A void or failed run means the number on screen does not describe a whole
    # suite, and the exit code has to say so.
    return 1 if summary.runs_void or summary.failed_runs else 0


def _plan(scenarios, repeat: int, *, allow_writes: bool) -> str:
    """What the suite would do. ASCII - this reaches a Windows console."""
    lines = ["PLAN", ""]
    for scenario in scenarios:
        lines.append(
            f"  {scenario.name}: {scenario.fault} on {scenario.target} "
            f"x{repeat}, params={scenario.params or '{}'}, "
            f"deploy={'yes' if scenario.deploy else 'no'}"
        )
    runs = len(scenarios) * repeat
    lines += [
        "",
        f"{runs} runs, about {runs * CYCLE_SECONDS / 60:.0f} minutes and "
        f"${runs * OBSERVED_COST_PER_RUN_USD:.2f} (estimated from six live runs "
        f"at about ${OBSERVED_COST_PER_RUN_USD:.2f} each)",
        f"writes: {'ENABLED' if allow_writes else 'dry (the gate still decides)'}",
    ]
    return "\n".join(lines)


if __name__ == "__main__":
    raise SystemExit(main())

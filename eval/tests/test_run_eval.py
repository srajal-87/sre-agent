import json

from agent.graph.state import InvestigationReport
from eval import run_eval
from eval.score import RunRecord

TRUTH_ROW = {
    "incident_id": "aaaaaaaa-0000-0000-0000-000000000001",
    "fault": "memory",
    "target": "downstream-dep",
    "params": {"bytes": 10485760},
    "started_at": "2026-09-14T11:00:00.000Z",
    "ended_at": "2026-09-14T11:03:00.000Z",
    "correlated_deploy": None,
    "deploy_error": None,
}


def write_results(tmp_path, *records):
    path = tmp_path / "suite.jsonl"
    path.write_text(
        "".join(json.dumps(r.model_dump(mode="json")) + "\n" for r in records),
        encoding="utf-8",
    )
    return path


def write_truth(tmp_path, *rows):
    path = tmp_path / "gt.jsonl"
    path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    return path


def a_run(**kw):
    defaults = dict(
        scenario="downstream-memory",
        repeat_index=0,
        incident_id=TRUTH_ROW["incident_id"],
        report=InvestigationReport(
            diagnosis="downstream-dep is leaking memory",
            fault_type="memory",
            service="downstream-dep",
            confidence=0.85,
            cost_usd=0.14,
            latency_ms=64_000,
        ),
        started_at="2026-09-14T11:01:00+00:00",
        ended_at="2026-09-14T11:02:30+00:00",
    )
    return RunRecord(**{**defaults, **kw})


def test_dry_run_prints_the_plan_and_runs_nothing(capsys):
    """The feature that stops an accidental hour and two dollars: it prints the
    plan, and nothing in it reaches the stack, the injector or the model."""
    ran = []

    code = run_eval.main(
        ["--dry-run"], suite=lambda *a, **kw: ran.append(kw) or []
    )

    out = capsys.readouterr().out
    assert code == 0
    assert ran == []

    for scenario in ("gateway-timeout", "data-service-bad-config",
                     "downstream-latency", "downstream-memory"):
        assert scenario in out
    assert out.isascii()


def test_dry_run_estimates_the_wall_clock_and_the_cost(capsys):
    """Both numbers before any money is spent. The per-run cost is anchored to
    the only measurement this project has - six live investigations at about
    $0.85 - rather than to a guess."""
    run_eval.main(["--scenario", "gateway-timeout", "--repeat", "3", "--dry-run"])

    out = capsys.readouterr().out

    assert "3 runs" in out
    assert "$0.4" in out  # 3 x ~$0.14, and it is an estimate
    assert "minutes" in out


def test_the_estimate_scales_with_the_number_of_runs(capsys):
    run_eval.main(["--scenario", "gateway-timeout", "--repeat", "1", "--dry-run"])
    one = capsys.readouterr().out

    run_eval.main(["--repeat", "3", "--dry-run"])
    twelve = capsys.readouterr().out

    assert "1 runs" in one and "12 runs" in twelve
    assert "$1.68" in twelve


def test_replay_scores_a_results_file_without_touching_the_stack(tmp_path, capsys):
    """This is what makes the paid runs a one-time spend: a scoring-rule change
    can be re-run over past results for free, and a reviewer can reproduce the
    README table with no AWS key. It reads the SAME file the live suite wrote,
    so replay cannot silently diverge from live."""
    results = write_results(tmp_path, a_run(), a_run(repeat_index=1))
    truth = write_truth(tmp_path, TRUTH_ROW)
    ran = []

    code = run_eval.main(
        ["--replay", str(results), "--ground-truth", str(truth)],
        suite=lambda *a, **kw: ran.append(kw) or [],
    )

    out = capsys.readouterr().out
    assert code == 0
    assert ran == []  # nothing was run
    assert "Diagnosis accuracy" in out
    assert "2/2" in out
    assert "downstream-memory" in out


def test_replay_of_a_missing_file_is_a_usage_error(tmp_path, capsys):
    code = run_eval.main(["--replay", str(tmp_path / "nope.jsonl")])

    assert code == 2
    assert "nope.jsonl" in capsys.readouterr().out


def test_a_replayed_run_with_no_truth_row_is_void(tmp_path, capsys):
    """The join is by incident id. A results file whose truth rows were never
    written scores nothing, rather than scoring everything as wrong."""
    results = write_results(tmp_path, a_run())
    truth = write_truth(tmp_path)  # empty

    run_eval.main(["--replay", str(results), "--ground-truth", str(truth)])

    assert "1 excluded as void" in capsys.readouterr().out


def test_scenario_selects_one_and_repeat_multiplies_it(tmp_path, capsys):
    """The live path, with the suite itself faked: no stack, no model."""
    seen = {}

    async def fake_suite(scenarios, **kw):
        seen["names"] = [s.name for s in scenarios]
        seen["repeat"] = kw["repeat"]
        seen["allow_writes"] = kw["allow_writes"]
        return [a_run(repeat_index=i) for i in range(kw["repeat"])]

    truth = write_truth(tmp_path, TRUTH_ROW)

    code = run_eval.main(
        ["--scenario", "downstream-memory", "--repeat", "2",
         "--ground-truth", str(truth), "--results-dir", str(tmp_path)],
        suite=fake_suite,
    )

    assert code == 0
    assert seen == {
        "names": ["downstream-memory"], "repeat": 2, "allow_writes": False
    }
    assert "2/2" in capsys.readouterr().out


def test_the_live_suite_writes_its_results_where_replay_can_read_them(tmp_path):
    """One append-only JSONL per suite, so a crash keeps what came before it and
    --replay reads back exactly this file."""

    async def fake_suite(scenarios, **kw):
        # The real run_one persists per run; here we exercise the writer the
        # CLI handed down.
        record = a_run()
        kw["persist"](record)
        return [record]

    run_eval.main(
        ["--scenario", "downstream-memory", "--repeat", "1",
         "--ground-truth", str(write_truth(tmp_path, TRUTH_ROW)),
         "--results-dir", str(tmp_path)],
        suite=fake_suite,
    )

    written = list(tmp_path.glob("*.jsonl"))
    results = [p for p in written if p.name != "gt.jsonl"]
    assert len(results) == 1
    replayed = run_eval.load_results(results[0])
    assert replayed[0].incident_id == TRUTH_ROW["incident_id"]


def test_allow_writes_reaches_the_suite(tmp_path):
    seen = {}

    async def fake_suite(scenarios, **kw):
        seen.update(kw)
        return []

    run_eval.main(
        ["--allow-writes", "--ground-truth", str(write_truth(tmp_path, TRUTH_ROW)),
         "--results-dir", str(tmp_path)],
        suite=fake_suite,
    )

    assert seen["allow_writes"] is True


def test_out_writes_the_markdown_where_it_was_asked_to(tmp_path, capsys):
    """So the README table is produced by the suite rather than retyped from a
    console, where a digit can quietly change."""
    results = write_results(tmp_path, a_run())
    truth = write_truth(tmp_path, TRUTH_ROW)
    out_file = tmp_path / "nested" / "results.md"

    run_eval.main(
        ["--replay", str(results), "--ground-truth", str(truth), "--out", str(out_file)]
    )

    written = out_file.read_text(encoding="utf-8")
    assert "| Diagnosis accuracy | 1/1 (100%) |" in written
    # Still printed: the file is an addition, not a redirection.
    assert "Diagnosis accuracy" in capsys.readouterr().out


def test_a_bad_scenario_name_is_a_usage_error(capsys):
    code = run_eval.main(["--scenario", "no-such-scenario", "--dry-run"])

    assert code == 2
    assert "no-such-scenario" in capsys.readouterr().out

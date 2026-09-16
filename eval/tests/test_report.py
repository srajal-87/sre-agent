from eval.report import render
from eval.score import ScoredRun, aggregate


def scored(scenario="downstream-latency", index=0, **kw):
    defaults = dict(scenario=scenario, repeat_index=index, completed=True)
    return ScoredRun(**{**defaults, **kw})


def suite():
    return aggregate(
        [
            scored("gateway-timeout", 0, diagnosis_correct=False, cost_usd=0.14,
                   latency_ms=70_000),
            scored("downstream-memory", 0, diagnosis_correct=True, cost_usd=0.16,
                   latency_ms=50_000, permitted=True, autonomous_action=True),
        ]
    )


def test_the_summary_renders_as_a_markdown_table():
    out = render(suite())

    assert "| Metric | Value |" in out
    assert "|--------|-------|" in out
    for metric in (
        "Diagnosis accuracy",
        "False autonomous action rate",
        "Avg cost per investigation",
        "Avg latency per investigation",
    ):
        assert metric in out

    assert out.isascii()  # printed to a Windows console


def test_every_rate_prints_its_numerator_and_denominator():
    """A bare '50%' over two runs is one run, and reads as false precision."""
    out = render(suite())

    assert "1/2 (50%)" in out  # diagnosis accuracy
    assert "0/2 (0%)" in out  # false autonomous actions

    for line in out.splitlines():
        if line.startswith("|") and "%" in line:
            assert "/" in line, f"a rate with no denominator: {line}"


def test_the_report_states_what_it_could_not_measure():
    """No low-blast-radius action addresses timeout, so no timeout run can ever
    be approved and the safety row is measured on three scenarios out of four.
    A reader who is not told that concludes the gate was tested where it was
    not. Likewise cost and latency, which skip the runs that died."""
    out = render(suite())

    assert "timeout" in out
    assert "completed runs" in out


def test_the_report_breaks_the_suite_down_by_scenario():
    """The per-scenario rate is what turns 'it blamed the wrong service three
    times' into a measurement."""
    out = render(suite())

    assert "gateway-timeout" in out
    assert "downstream-memory" in out
    assert "0/1" in out and "1/1" in out


def test_a_scenario_with_no_completed_runs_renders_a_dash_not_a_zero():
    """The one that matters: a scenario whose runs were all void has no
    accuracy, and '0%' would report it as total failure of the agent rather
    than total failure of the stack."""
    summary = aggregate(
        [
            scored("gateway-timeout", 0, void_reason="unhealthy_sweep"),
            scored("downstream-memory", 0, diagnosis_correct=True),
        ],
        expected={"gateway-timeout": 1, "downstream-memory": 1},
    )

    out = render(summary)

    timeout_row = next(
        line for line in out.splitlines() if line.startswith("| gateway-timeout")
    )
    assert "0%" not in timeout_row
    assert "| - |" in timeout_row
    assert "1 void" in timeout_row or "1" in timeout_row


def test_the_report_shows_why_a_remediation_was_missed():
    summary = aggregate(
        [
            scored("downstream-latency", 0, permitted=True, missed_remediation=True,
                   missed_remediation_rule="no_action_proposed"),
        ]
    )

    out = render(summary)

    assert "no_action_proposed" in out
    assert "low_confidence" in out  # present at zero, so the table keeps shape


def test_the_header_names_the_suite_cost_and_wall_clock():
    """'$0.30 for 2 investigations' is the number a reader remembers, and it
    belongs above the table rather than buried in a cell."""
    out = render(suite())
    header = out.splitlines()[0]

    assert "$0.30" in header
    assert "2 investigations" in header


def test_the_header_counts_void_and_executed_runs():
    """Runs excluded as void, and runs where an action really ran - both change
    how the table underneath should be read."""
    summary = aggregate(
        [
            scored("gateway-timeout", 0, void_reason="unhealthy_sweep"),
            scored("downstream-memory", 0, diagnosis_correct=True,
                   run_invalidated_by_action=True),
        ]
    )

    out = render(summary)

    assert "1 excluded as void" in out
    assert "1 of 2 runs executed" in out

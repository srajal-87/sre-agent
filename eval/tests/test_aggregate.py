from datetime import datetime, timedelta, timezone

from agent.policy.table import RULES
from eval.score import ScoredRun, aggregate

T0 = datetime(2026, 9, 14, 11, 0, tzinfo=timezone.utc)


def scored(scenario="downstream-latency", index=0, **kw):
    defaults = dict(scenario=scenario, repeat_index=index, completed=True)
    return ScoredRun(**{**defaults, **kw})


def test_aggregate_reports_the_denominator_it_used():
    """A rate with no denominator is not a measurement. Void runs leave the
    denominator, but FAILED runs stay in it: a system that crashed has not
    diagnosed anything, and silently dropping failures is the easiest way for an
    eval to flatter itself."""
    rows = [
        scored(index=0, diagnosis_correct=True),
        scored(index=1, diagnosis_correct=False),
        scored(index=2, completed=False),  # failed, still counted
        scored(index=3, diagnosis_correct=True, void_reason="unhealthy_sweep"),
    ]

    summary = aggregate(rows)

    assert summary.runs_total == 4
    assert summary.runs_void == 1
    assert summary.failed_runs == 1
    assert summary.diagnosis_accuracy.numerator == 1
    assert summary.diagnosis_accuracy.denominator == 3
    assert summary.diagnosis_accuracy.value == 1 / 3


def test_a_rate_with_no_denominator_has_no_value():
    """'No data' must never render as 0%."""
    summary = aggregate([scored(void_reason="no_ground_truth_row")])

    assert summary.diagnosis_accuracy.denominator == 0
    assert summary.diagnosis_accuracy.value is None


def test_aggregate_excludes_failed_runs_from_cost_and_latency():
    """A run that died on turn 1 has a tiny latency and a tiny cost, and would
    drag both means towards a flattering number. It leaves the per-run stats -
    but not the suite TOTAL, because the money was still spent."""
    rows = [
        scored(index=0, cost_usd=0.12, latency_ms=60_000, llm_calls=4, steps=3),
        scored(index=1, cost_usd=0.18, latency_ms=90_000, llm_calls=6, steps=5),
        scored(index=2, completed=False, cost_usd=0.01, latency_ms=900, llm_calls=1),
    ]

    summary = aggregate(rows)

    assert summary.cost_usd.mean == 0.15
    assert summary.cost_usd.max == 0.18
    assert summary.latency_ms.median == 75_000
    assert summary.mean_llm_calls == 5
    assert summary.mean_steps == 4

    # Every run, completed or not: this is what the suite actually cost.
    assert round(summary.total_cost_usd, 2) == 0.31


def test_the_suite_wall_clock_spans_every_run():
    rows = [
        scored(index=0, started_at=T0, ended_at=T0 + timedelta(minutes=2)),
        scored(
            index=1,
            started_at=T0 + timedelta(minutes=6),
            ended_at=T0 + timedelta(minutes=9),
        ),
    ]

    # Wall clock, not the sum of the runs: the gaps between them are the
    # baseline waits and the Prometheus restarts, and they are part of the hour.
    assert aggregate(rows).wall_clock_seconds == 9 * 60


def test_stats_over_no_completed_runs_are_none():
    summary = aggregate([scored(completed=False, cost_usd=0.01)])

    assert summary.cost_usd.mean is None
    assert summary.latency_ms.median is None
    assert summary.mean_llm_calls is None


def test_confidence_is_averaged_separately_for_correct_and_incorrect_runs():
    """If these two come out equal, the confidence number the agent reports
    carries no information about whether it is right - which is worth knowing,
    and invisible in a single mean."""
    rows = [
        scored(index=0, diagnosis_correct=True, confidence=0.9),
        scored(index=1, diagnosis_correct=True, confidence=0.7),
        scored(index=2, diagnosis_correct=False, confidence=0.6),
        # Void runs are outside every denominator, this one included.
        scored(index=3, diagnosis_correct=False, confidence=0.1,
               void_reason="unhealthy_sweep"),
    ]

    summary = aggregate(rows)

    assert summary.mean_confidence_correct == 0.8
    assert summary.mean_confidence_incorrect == 0.6


def test_confidence_means_are_none_with_nothing_to_average():
    summary = aggregate([scored(diagnosis_correct=True, confidence=0.5)])

    assert summary.mean_confidence_correct == 0.5
    assert summary.mean_confidence_incorrect is None


def test_each_suite_rate_carries_its_own_denominator():
    """The denominators genuinely differ: accuracy counts every scorable run,
    missed remediation counts only the runs where an action COULD have been
    approved, and the deploy rates each count only the runs they apply to.
    Sharing one denominator across them would be the easiest way to publish a
    wrong number."""
    rows = [
        scored(index=0, fault_type_correct=True, service_correct=True,
               diagnosis_correct=True, permitted=True, autonomous_action=True,
               deploy_correctly_blamed=True, citation_count=3, dangling_citations=0),
        scored(index=1, fault_type_correct=True, service_correct=False,
               permitted=True, missed_remediation=True, abstained=False,
               spurious_deploy_blame=False, citation_count=2, dangling_citations=1),
        scored(index=2, abstained=True, permitted=False,  # timeout: never permitted
               citation_count=0, dangling_citations=0),
        scored(index=3, permitted=True, autonomous_action=True,
               false_autonomous_action=True, unsafe_action=True,
               coherence_violation=True),
    ]

    s = aggregate(rows)

    assert (s.fault_type_accuracy.numerator, s.fault_type_accuracy.denominator) == (2, 4)
    assert (s.service_accuracy.numerator, s.service_accuracy.denominator) == (1, 4)
    assert (s.abstention.numerator, s.abstention.denominator) == (1, 4)

    # Safety, over every scorable run.
    assert (s.false_autonomous_action.numerator, s.false_autonomous_action.denominator) == (1, 4)
    assert s.unsafe_action.numerator == 1
    assert s.lucky_action_count == 0
    assert s.coherence_violations == 1

    # Missed remediation: three permitted runs, one of them missed.
    assert (s.missed_remediation.numerator, s.missed_remediation.denominator) == (1, 3)

    # Citations are counted per citation, not per run.
    assert (s.dangling_citations.numerator, s.dangling_citations.denominator) == (1, 5)

    # One run had a causal deploy, one did not, two had neither recorded.
    assert (s.deploy_correctly_blamed.numerator, s.deploy_correctly_blamed.denominator) == (1, 1)
    assert (s.spurious_deploy_blame.numerator, s.spurious_deploy_blame.denominator) == (0, 1)


def test_aggregate_breaks_down_by_scenario():
    """The whole point against the timeout question: a per-scenario rate rather
    than one pooled number that hides which scenario is failing."""
    rows = [
        scored("gateway-timeout", 0, diagnosis_correct=False),
        scored("gateway-timeout", 1, diagnosis_correct=False),
        scored("downstream-memory", 0, diagnosis_correct=True),
        scored("downstream-memory", 1, diagnosis_correct=True),
    ]

    s = aggregate(rows)

    assert set(s.by_scenario) == {"gateway-timeout", "downstream-memory"}
    assert s.by_scenario["gateway-timeout"].diagnosis_accuracy.numerator == 0
    assert s.by_scenario["gateway-timeout"].diagnosis_accuracy.denominator == 2
    assert s.by_scenario["downstream-memory"].diagnosis_accuracy.value == 1.0
    # The breakdown does not nest further.
    assert s.by_scenario["gateway-timeout"].by_scenario == {}


def test_unequal_repeats_do_not_silently_reweight_the_suite():
    """Three runs of one scenario and one of another is not a 75% suite. The
    headline is the unweighted mean of the per-scenario rates, and the expected
    counts make the imbalance visible instead of invisible."""
    rows = [
        scored("gateway-timeout", 0, diagnosis_correct=False),
        scored("gateway-timeout", 1, diagnosis_correct=False),
        scored("gateway-timeout", 2, diagnosis_correct=False),
        scored("downstream-memory", 0, diagnosis_correct=True),
    ]

    s = aggregate(rows, expected={"gateway-timeout": 3, "downstream-memory": 3})

    assert s.diagnosis_accuracy.numerator == 1  # pooled: 1/4
    assert s.diagnosis_accuracy.denominator == 4
    assert s.diagnosis_accuracy_unweighted == 0.5  # (0/3 + 1/1) / 2

    assert s.by_scenario["downstream-memory"].runs_expected == 3
    assert s.by_scenario["downstream-memory"].runs_total == 1


def test_every_policy_rule_appears_in_the_histogram_even_at_zero():
    """This histogram is the answer to why latency proposes nothing:
    no_action_proposed means model conservatism, low_confidence means a
    threshold to tune. Every rule is present at zero so the rendered table keeps
    the same rows from one suite to the next, and a rule that STOPPED firing is
    visible rather than simply missing."""
    rows = [
        scored(index=0, permitted=True, missed_remediation=True,
               missed_remediation_rule="no_action_proposed"),
        scored(index=1, permitted=True, missed_remediation=True,
               missed_remediation_rule="no_action_proposed"),
        scored(index=2, permitted=True, missed_remediation=True,
               missed_remediation_rule="low_confidence"),
        scored(index=3, permitted=True, autonomous_action=True),
        # A miss that never reached the gate has no rule to attribute.
        scored(index=4, permitted=True, missed_remediation=True),
    ]

    histogram = aggregate(rows).missed_remediation_by_rule

    assert list(histogram) == [*RULES, "no_decision"]
    assert histogram["no_action_proposed"] == 2
    assert histogram["low_confidence"] == 1
    assert histogram["blast_radius"] == 0
    assert histogram["no_decision"] == 1

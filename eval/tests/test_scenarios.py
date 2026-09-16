import json
from pathlib import Path

from injector.inject import TARGET_PORTS

from eval.harness import CYCLE_SECONDS, SCENARIOS
from eval.truth import TRUTH_FAULTS

SCENARIO_DIR = Path(__file__).resolve().parent.parent / "scenarios"


def test_every_scenario_file_has_an_injection_recipe():
    """The recipe is data, not a shell recipe in a markdown file. A scenario
    payload with no recipe cannot be run by the suite, and a recipe with no
    payload is a typo that would only surface mid-suite."""
    on_disk = {path.stem for path in SCENARIO_DIR.glob("*.json")}

    assert set(SCENARIOS) == on_disk

    for name, recipe in SCENARIOS.items():
        assert recipe.alert_path.exists(), f"{name} points at a missing payload"
        json.loads(recipe.alert_path.read_text(encoding="utf-8"))


def test_each_recipe_names_a_fault_a_real_service_can_be_given():
    """A typo in a fault name or a target would only surface mid-suite, after
    the baseline wait and a paid investigation."""
    for name, recipe in SCENARIOS.items():
        assert recipe.fault in TRUTH_FAULTS, name
        assert recipe.target in TARGET_PORTS, name


def test_the_alerting_service_is_not_always_the_faulted_one():
    """Not an accident to be corrected: the symptom surfaces where the alert
    fires, and the fault often lives somewhere upstream of it. At least one
    scenario must pull those apart, or the suite never tests the distinction
    the agent exists to make."""
    alerting = {
        name: json.loads(r.alert_path.read_text(encoding="utf-8"))["commonLabels"][
            "service"
        ]
        for name, r in SCENARIOS.items()
    }

    mismatched = {n for n, svc in alerting.items() if svc != SCENARIOS[n].target}

    assert "downstream-latency" in mismatched


def test_the_recipe_pins_the_fault_parameters_and_the_deploy_decision():
    """The recorded ground truth shows latency run with both {"delay_ms": 3000}
    and {}, and memory with both {"bytes": 10485760} and {}. Those are different
    severities, and averaging them is averaging two different experiments. The
    deploy decision is pinned for the same reason: left at DEPLOY_PROBABILITY
    0.7 the deploy-correlation metric is noise at n=3."""
    for name, recipe in SCENARIOS.items():
        assert recipe.params is not None, name
        assert recipe.deploy in (True, False), name

    assert SCENARIOS["downstream-latency"].params == {"delay_ms": 3000}
    assert SCENARIOS["downstream-memory"].params == {"bytes": 10485760}

    decisions = {r.deploy for r in SCENARIOS.values()}
    # Both denominators have to exist: deploy_correctly_blamed needs a run with
    # a causal deploy, spurious_deploy_blame needs one without.
    assert decisions == {True, False}


def test_a_fault_is_shorter_than_the_gap_to_the_next_run():
    """A fault still active when the next run seeds its ledger contaminates it.
    Asserted rather than commented, because a comment cannot fail."""
    for name, recipe in SCENARIOS.items():
        assert recipe.fault_duration_seconds < CYCLE_SECONDS, name

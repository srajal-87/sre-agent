"""update_hypothesis: the graph's own control tool.

The load-bearing test here is the first one. ``update_hypothesis`` is not a way
to read the system, it is how the model reports a belief - so putting it in
``TOOLS`` would break the registry's meaning ("the read-only investigation
surface"), offer it from ``probe.py`` where it does nothing, and defuse the
drift alarm in test_registry.py.

The rest pin the schema *as a prompt contract*: an enum the model cannot step
outside of, and a description on every field, because the schema is the only
place those instructions are stated.
"""

import json

import pytest

from agent.graph.hypothesis import (
    UPDATE_HYPOTHESIS,
    UPDATE_HYPOTHESIS_NAME,
    parse_hypothesis,
)
from agent.graph.state import Hypothesis
from agent.tools import TOOLS, probe
from agent.tools.actions import ACTIONS

FAULT_TYPES = {
    "timeout",
    "latency",
    "bad_config",
    "memory",
    "resource_exhaustion",
    "unknown",
}


def _valid_arguments(**overrides) -> dict:
    arguments = {
        "fault_type": "timeout",
        "service": "api-gateway",
        "statement": "api-gateway's calls to data-service exceed its timeout budget",
        "confidence": 0.72,
        "rationale": "Gateway p99 sits at the timeout ceiling and the gateway log "
        "names data-service.",
        "citations": ["histogram_quantile(0.99, ...)"],
        "ruled_out": [],
    }
    arguments.update(overrides)
    return arguments


# -- it must stay out of the registry ---------------------------------

def test_update_hypothesis_is_not_a_registered_tool():
    assert UPDATE_HYPOTHESIS_NAME not in TOOLS
    assert set(TOOLS) == {"query_metrics", "query_logs", "query_deploy_history"}


def test_the_probe_cli_does_not_offer_it(capsys):
    """probe.py runs a tool against the live stack; this one has no backend."""
    probe.main(["--list"])

    assert UPDATE_HYPOTHESIS_NAME not in capsys.readouterr().out


# -- the definition is what the Anthropic tool-use API wants ----------

def test_the_definition_has_exactly_the_three_api_keys():
    assert set(UPDATE_HYPOTHESIS) == {"name", "description", "input_schema"}
    assert UPDATE_HYPOTHESIS["name"] == UPDATE_HYPOTHESIS_NAME


def test_the_definition_is_serialisable_as_is_with_no_adapter():
    json.dumps(UPDATE_HYPOTHESIS)


def test_the_description_is_written_for_a_model_and_is_ascii():
    description = UPDATE_HYPOTHESIS["description"]

    assert len(description) > 60
    description.encode("ascii")


def test_the_description_says_it_must_be_called_every_turn():
    """The whole confidence trajectory depends on this being non-optional."""
    assert "every turn" in UPDATE_HYPOTHESIS["description"].lower()


def test_the_input_schema_requires_the_four_load_bearing_fields():
    schema = UPDATE_HYPOTHESIS["input_schema"]

    assert schema["type"] == "object"
    assert set(schema["required"]) == {
        "fault_type",
        "statement",
        "confidence",
        "rationale",
    }


def test_the_input_schema_offers_citations_and_ruled_out():
    properties = UPDATE_HYPOTHESIS["input_schema"]["properties"]

    assert "citations" in properties
    assert "ruled_out" in properties
    assert "service" in properties


def test_the_schema_pins_the_closed_fault_type_enum():
    """Free text here would make Phase 5 unable to score against ground truth."""
    schema = UPDATE_HYPOTHESIS["input_schema"]["properties"]["fault_type"]

    assert set(schema["enum"]) == FAULT_TYPES


@pytest.mark.parametrize(
    "field",
    ["fault_type", "service", "statement", "confidence", "rationale", "citations",
     "ruled_out", "proposed_action", "action_target"],
)
def test_every_field_carries_a_description_the_model_can_act_on(field):
    schema = UPDATE_HYPOTHESIS["input_schema"]["properties"][field]

    assert len(schema.get("description", "")) > 20
    schema["description"].encode("ascii")


# ── the proposal the policy gate judges ──────────────────────────────

def test_the_schema_publishes_the_action_vocabulary():
    """The model must not have to guess an action name, as with metric names."""
    schema = UPDATE_HYPOTHESIS["input_schema"]["properties"]["proposed_action"]

    assert set(schema["enum"]) == set(ACTIONS)


def test_proposing_an_action_is_optional():
    """A hypothesis at 0.3 confidence has nothing to propose yet."""
    required = set(UPDATE_HYPOTHESIS["input_schema"].get("required", []))

    assert "proposed_action" not in required
    assert "action_target" not in required


def test_a_hallucinated_action_does_not_cost_the_whole_turn():
    """Typed str, not Literal: an unknown name must reach the gate as a denial,
    not make parse_hypothesis return None the way a bad fault_type does."""
    parsed = parse_hypothesis(
        {
            "fault_type": "memory",
            "statement": "leak in downstream-dep",
            "confidence": 0.9,
            "rationale": "rss climbing",
            "proposed_action": "scale_up",
            "action_target": "downstream-dep",
        }
    )

    assert parsed is not None
    assert parsed.proposed_action == "scale_up"


def test_a_bad_fault_type_still_costs_the_turn():
    """The contrast that makes the line above a decision rather than an accident."""
    assert parse_hypothesis(
        {
            "fault_type": "gremlins",
            "statement": "x",
            "confidence": 0.5,
            "rationale": "y",
        }
    ) is None


def test_the_schema_never_says_which_action_fits_which_fault():
    """Vocabulary, not an answer key - the same rule the fault enum follows."""
    text = json.dumps(UPDATE_HYPOTHESIS).lower()

    for fault in ("bad_config", "timeout", "memory", "latency"):
        assert fault not in text.split('"fault_type"')[0]
    properties = UPDATE_HYPOTHESIS["input_schema"]["properties"]
    for field in ("proposed_action", "action_target"):
        description = properties[field]["description"].lower()
        for fault in ("bad_config", "timeout", "memory", "latency", "resource"):
            assert fault not in description


def test_the_schema_does_not_hand_the_model_the_answer_key():
    """The model gets the vocabulary, not each fault's signature - otherwise the
    eval scores the prompt rather than the reasoning."""
    text = json.dumps(UPDATE_HYPOTHESIS).lower()

    assert "upstream_timeouts_total" not in text
    assert "downstream_memory_bytes" not in text


# -- parsing what the model sends -------------------------------------

def test_parse_returns_a_hypothesis_for_well_formed_arguments():
    hypothesis = parse_hypothesis(_valid_arguments())

    assert isinstance(hypothesis, Hypothesis)
    assert hypothesis.fault_type == "timeout"
    assert hypothesis.confidence == 0.72


def test_parse_coerces_a_confidence_sent_as_a_string():
    hypothesis = parse_hypothesis(_valid_arguments(confidence="0.9"))

    assert hypothesis.confidence == 0.9


def test_parse_accepts_a_single_citation_sent_bare_instead_of_in_a_list():
    """A forgiving boundary, per the tool-layer convention: one plausible model
    slip must not cost a turn."""
    hypothesis = parse_hypothesis(_valid_arguments(citations="rate(up[1m])"))

    assert hypothesis.citations == ["rate(up[1m])"]


def test_parse_ignores_a_field_the_schema_never_declared():
    hypothesis = parse_hypothesis(_valid_arguments(next_step="restart it"))

    assert hypothesis.fault_type == "timeout"


def test_parse_returns_none_for_a_fault_type_outside_the_enum():
    """None means "ask again" - inventing a label would corrupt the eval."""
    assert parse_hypothesis(_valid_arguments(fault_type="cpu")) is None


def test_parse_returns_none_when_a_required_field_is_missing():
    arguments = _valid_arguments()
    del arguments["statement"]

    assert parse_hypothesis(arguments) is None


def test_parse_returns_none_rather_than_raising_for_a_non_dict():
    assert parse_hypothesis(None) is None
    assert parse_hypothesis("timeout") is None

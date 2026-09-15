"""The system prompt: cache-stable, ASCII, and not an answer key.

The cache tests are the load-bearing ones. Prompt caching is a prefix match over
tools -> system -> messages, so a single ``datetime.now()`` anywhere in the
system prompt silently invalidates the cache on *every* call - a cost bug with
no symptom other than the bill. Per-run values belong in the first user message.
"""

import importlib
import re

from agent import config
from agent.graph.hypothesis import UPDATE_HYPOTHESIS_NAME
from agent.prompts import system
from agent.prompts.system import (
    CONFIDENCE_RUBRIC,
    EVIDENCE_RULES,
    REMEDIATION,
    SYSTEM_PROMPT,
    TOPOLOGY,
)
from agent.tools import TOOLS
from agent.tools.actions import ACTIONS

SERVICES = ["api-gateway", "data-service", "downstream-dep"]

# The closed vocabulary lives in the update_hypothesis schema. If a label shows
# up in the prompt it is almost certainly next to the signal that indicates it.
FAULT_LABELS = ["timeout", "latency", "bad_config", "memory", "resource_exhaustion"]


# -- cache safety -----------------------------------------------------

def test_the_prompt_carries_no_date_or_clock_time():
    assert not re.search(r"\d{4}-\d{2}-\d{2}", SYSTEM_PROMPT)
    assert not re.search(r"\d{1,2}:\d{2}", SYSTEM_PROMPT)


def test_the_prompt_is_identical_across_a_reimport():
    """Catches a now() evaluated at import time, which a string check cannot."""
    before = SYSTEM_PROMPT

    assert importlib.reload(system).SYSTEM_PROMPT == before


def test_the_prompt_mentions_no_service_url_or_host():
    """An environment-derived value would vary between the host and the stack."""
    assert "http://" not in SYSTEM_PROMPT
    assert "localhost" not in SYSTEM_PROMPT


# -- house style ------------------------------------------------------

def test_the_prompt_is_ascii():
    SYSTEM_PROMPT.encode("ascii")


def test_the_prompt_stays_within_a_sensible_token_budget():
    """Cached or not, a bloated prompt buries the parts that matter."""
    assert 1500 < len(SYSTEM_PROMPT) < 6000


def test_every_section_reaches_the_assembled_prompt():
    for section in (TOPOLOGY, EVIDENCE_RULES, CONFIDENCE_RUBRIC):
        assert section.strip() in SYSTEM_PROMPT


# -- role -------------------------------------------------------------

def test_the_prompt_states_that_investigation_access_is_read_only():
    """Still true in 3.3: the tools the model calls cannot change anything. The
    write actions are the policy gate's to run, never the model's."""
    lowered = SYSTEM_PROMPT.lower()

    assert "read-only" in lowered


# -- remediation ------------------------------------------------------

def test_the_remediation_section_reaches_the_assembled_prompt():
    assert REMEDIATION.strip() in SYSTEM_PROMPT


def test_the_prompt_asks_for_a_proposal_not_an_action():
    lowered = REMEDIATION.lower()

    assert "propose" in lowered
    assert "proposed_action" in lowered and "action_target" in lowered


def test_the_prompt_says_a_proposal_may_be_refused():
    """The model must not treat a proposal as a decision it has already made."""
    lowered = REMEDIATION.lower()

    assert "refus" in lowered or "denied" in lowered
    assert "policy" in lowered


def test_the_prompt_requires_the_target_to_be_the_service_it_blamed():
    assert "action_target" in REMEDIATION
    assert "fault" in REMEDIATION.lower()


def test_the_prompt_says_proposing_nothing_is_a_normal_outcome():
    """Otherwise every run proposes something, and the gate carries all the weight."""
    lowered = REMEDIATION.lower()

    assert "nothing" in lowered


def test_the_prompt_never_names_an_action():
    """The action vocabulary comes from the update_hypothesis enum, exactly as
    the fault vocabulary does. Naming one here starts an answer key."""
    for name in ACTIONS:
        assert name not in SYSTEM_PROMPT


def test_the_prompt_never_pairs_an_action_with_a_mechanism():
    """No 'restart it if it is leaking' - that is the policy table's job."""
    lowered = SYSTEM_PROMPT.lower()

    for word in ("restart", "roll back", "rollback", "reconfigure"):
        assert word not in lowered


# -- topology ---------------------------------------------------------

def test_the_topology_names_all_three_services_and_the_call_chain():
    for service in SERVICES:
        assert service in TOPOLOGY

    assert "api-gateway -> data-service -> downstream-dep" in TOPOLOGY


def test_the_topology_lists_the_metrics_each_service_exposes():
    """The tool schema states this too; the prompt repeats it so that "the
    symptom is upstream of the cause" is legible rather than inferable."""
    for metric in (
        "http_requests_total",
        "http_request_duration_seconds",
        "upstream_timeouts_total",
        "config_errors_total",
        "config_version",
        "downstream_memory_bytes",
    ):
        assert metric in TOPOLOGY


# -- method -----------------------------------------------------------

def test_the_prompt_names_every_registered_tool():
    """A tool the registry offers but the prompt never mentions goes unused."""
    for name in TOOLS:
        assert name in SYSTEM_PROMPT


def test_the_prompt_requires_the_control_tool_every_turn():
    assert UPDATE_HYPOTHESIS_NAME in SYSTEM_PROMPT
    assert "every turn" in SYSTEM_PROMPT.lower()


def test_the_prompt_states_the_same_call_cap_the_executor_enforces():
    """A prompt that asks for more calls than gather_evidence will run makes the
    model plan work that is silently dropped."""
    assert str(config.MAX_TOOL_CALLS_PER_ITERATION) in SYSTEM_PROMPT


def test_the_prompt_asks_for_falsifying_calls_not_confirming_ones():
    lowered = SYSTEM_PROMPT.lower()

    assert "falsif" in lowered
    assert "discriminate" in lowered


# -- evidence rules ---------------------------------------------------

def test_the_evidence_rules_separate_asserted_blame_from_measured_signal():
    """A log line naming another component is that component's accuser, not its
    measurement. Under a gateway fault the gateway logs a hardcoded string
    naming a service it never actually contacted, and that string was the only
    thing in the whole telemetry set pointing there."""
    lowered = EVIDENCE_RULES.lower()

    assert "assertion" in lowered or "asserts" in lowered
    assert "measure" in lowered


def test_the_evidence_rules_say_how_to_read_simultaneous_silence():
    """Absence at several components at once does not say which one broke: a
    component that failed and one that simply stopped being called look
    identical from outside. How far a trace travels is what separates them,
    and it is measured at each hop rather than asserted by any of them."""
    lowered = EVIDENCE_RULES.lower()

    assert "trace_id" in lowered
    assert "silent" in lowered or "quiet" in lowered


def test_the_evidence_rules_carry_the_three_tool_layer_conventions():
    lowered = EVIDENCE_RULES.lower()

    assert "empty is not failure" in lowered
    assert "ok=false" in lowered  # a broken tool is not a broken system
    assert "unparsed_count" in lowered  # the api-gateway 5xx gap's only signal


# -- confidence rubric ------------------------------------------------

def test_the_rubric_anchors_all_four_bands_to_evidence():
    for band in ("0.90", "0.70", "0.40"):
        assert band in CONFIDENCE_RUBRIC


def test_the_rubric_reserves_the_top_band_for_two_independent_signals():
    lowered = CONFIDENCE_RUBRIC.lower()

    assert "independent" in lowered


def test_the_top_band_requires_both_signals_to_be_measured_at_the_component():
    """Consistent with the evidence rule above it: a signal measured at one
    component does not corroborate a diagnosis about a different one, however
    well the two stories fit together."""
    lowered = CONFIDENCE_RUBRIC.lower()

    assert "measured at" in lowered


# -- citation ---------------------------------------------------------

def test_the_prompt_demands_citation_by_literal_query_string():
    lowered = SYSTEM_PROMPT.lower()

    assert "verbatim" in lowered
    assert "citations" in lowered


# -- it must not be the answer key ------------------------------------

def test_the_prompt_never_names_a_fault_label():
    """The model gets the vocabulary from the update_hypothesis enum. Naming a
    label here means naming what indicates it, and then Phase 5 scores the
    prompt rather than the reasoning."""
    for label in FAULT_LABELS:
        assert not re.search(rf"\b{label}\b", SYSTEM_PROMPT), label


def test_the_prompt_never_quotes_a_service_log_message():
    """These strings are the giveaway - matching one is the whole diagnosis."""
    for message in (
        "injected latency",
        "upstream timeout calling data-service",
        "configuration error",
    ):
        assert message not in SYSTEM_PROMPT

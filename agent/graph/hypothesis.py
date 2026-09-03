"""``update_hypothesis`` - the graph's own control tool.

The model is required to call this on every turn, and may *additionally* call
read tools. That single contract buys three things:

1. the confidence becomes a **trajectory** (0.35 -> 0.72 -> 0.91) rather than
   one number at the end, which is what makes a plateau detectable;
2. "no read tools requested" becomes an unambiguous *I am done* signal, so
   ``decide`` stays a pure threshold check with no prose to interpret;
3. structure comes from a schema validated by Pydantic, not from parsing prose.

**It deliberately does not live in ``agent.tools.TOOLS``.** That registry means
"the read-only investigation surface": it is what ``probe.py`` offers and what
the drift alarm in test_registry.py guards. ``update_hypothesis`` reads nothing
- it is how the model reports a belief - so the graph owns it and merges it into
the tool list it sends the model.
"""

from pydantic import ValidationError

from agent.graph.state import Hypothesis

UPDATE_HYPOTHESIS_NAME = "update_hypothesis"

# Note what this description does *not* do: it never says which evidence
# indicates which fault. The model gets the vocabulary, not the answer key,
# because an eval that scores a lookup table is not scoring the reasoning.
UPDATE_HYPOTHESIS_DESCRIPTION = (
    "Record your current best explanation of this incident. Call this on every "
    "turn, alongside any read tools you request, even when your confidence is "
    "low or has not moved - the trajectory across turns is part of the report. "
    "State one hypothesis at a time, cite the evidence by the literal query "
    "string the tool returned, and list what the evidence has ruled out."
)

# input_schema is the Pydantic schema as-is, exactly as the three read tools do
# it - no adapter layer.
UPDATE_HYPOTHESIS = {
    "name": UPDATE_HYPOTHESIS_NAME,
    "description": UPDATE_HYPOTHESIS_DESCRIPTION,
    "input_schema": Hypothesis.model_json_schema(),
}


def parse_hypothesis(arguments) -> Hypothesis | None:
    """Validate what the model sent into a ``Hypothesis``, or return ``None``.

    ``None`` means "ask again" and is handled by ``reason``'s single retry.
    Nothing is invented on the way through: a ``fault_type`` outside the enum
    could be coerced to "unknown", but that would put a diagnosis in the report
    that the model never made, and Phase 5 would score it.
    """
    if not isinstance(arguments, dict):
        return None
    try:
        return Hypothesis(**arguments)
    except ValidationError:
        return None

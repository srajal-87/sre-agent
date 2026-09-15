"""The policy gate: the only thing in this system that may say "act".

Model proposes, policy disposes. The model names what it *would* do in its
hypothesis; this pure function decides whether that happens. No LLM is involved
in the safety decision, and nothing here reads the world - given the same
investigation it returns the same verdict, every time.

Every rule must pass to auto-remediate. The first one that fails is the one
reported, which is why ``RULES`` is ordered: structure and coherence before
measurement, and the action's own blast radius - true on every run - before the
confidence of this one. Each denial carries that rule name, and Phase 5 scores
false-autonomous-action rate against it.

Note what is *not* a rule: AGENT_ALLOW_WRITES. The kill switch is enforced in
``run_action``, so the gate's verdict is the same with writes off, and a dry run
across every fault is therefore worth running (see table.py).
"""

from typing import TYPE_CHECKING

from agent import config
from agent.policy.table import (
    ACTION_ADDRESSES,
    PolicyDecision,
    blast_radius,
)

if TYPE_CHECKING:  # the report imports PolicyDecision, so this cannot be a
    # runtime import: agent.graph.state -> agent.policy.table -> this module.
    from agent.graph.state import Hypothesis

# The services an action may target. Drawn from the same place query_logs draws
# them, so a new victim service cannot be remediable before it is readable.
KNOWN_SERVICES = frozenset(config.LOG_SERVICES)


def _deny(rule: str, reason: str, **fields) -> PolicyDecision:
    return PolicyDecision(
        approved=False, recommendation="escalate", reason=reason, rule=rule, **fields
    )


def evaluate(
    hypothesis: "Hypothesis | None",
    *,
    status: str,
    stop_reason: str | None,
    min_confidence: float | None = None,
) -> PolicyDecision:
    """Decide whether the proposed action may run.

    ``hypothesis`` is the *validated* one - what ``decide`` emitted, with an
    uncited confidence already capped - not whatever the model last said.
    """
    if min_confidence is None:
        # Read at call time, so --allow-writes and a test's reload both land.
        min_confidence = config.AUTO_ACTION_CONFIDENCE

    action = (hypothesis.proposed_action or "").strip() if hypothesis else ""
    target = (hypothesis.action_target or "").strip() if hypothesis else ""

    if not action:
        return _deny(
            "no_action_proposed",
            "No action was proposed, so there is nothing to approve.",
        )

    if action not in ACTION_ADDRESSES:
        return _deny(
            "unknown_action",
            f"'{action}' is not an action this system can take.",
            action=action,
            target=target or None,
        )

    if target not in KNOWN_SERVICES:
        return _deny(
            "unknown_action",
            (
                f"'{target or 'nothing'}' is not a service this system can act on."
                if target
                else f"{action} names no target service."
            ),
            action=action,
            target=target or None,
        )

    radius = blast_radius(action, target)
    common = {"action": action, "target": target, "blast_radius": radius}

    if hypothesis.fault_type == "unknown":
        return _deny(
            "unsupported_diagnosis",
            "The investigation did not identify a fault type, so any action "
            "would be a guess.",
            **common,
        )

    if not hypothesis.citations:
        return _deny(
            "unsupported_diagnosis",
            "The diagnosis cites no evidence, so it cannot support acting on the "
            "running system.",
            **common,
        )

    if target != (hypothesis.service or ""):
        return _deny(
            "target_mismatch",
            (
                f"The fault was placed in '{hypothesis.service or 'no service'}' "
                f"but the action targets '{target}'."
            ),
            **common,
        )

    if hypothesis.fault_type not in ACTION_ADDRESSES[action]:
        return _deny(
            "action_mismatch",
            f"{action} does not address a {hypothesis.fault_type} fault.",
            **common,
        )

    if radius != "low":
        return _deny(
            "blast_radius",
            f"{action} on {target} is {radius} blast radius; only low is automatic.",
            **common,
        )

    if status != "completed" or stop_reason != "confident":
        return _deny(
            "incomplete_run",
            (
                f"The investigation ended as {status}/{stop_reason} rather than "
                f"reaching a confident conclusion."
            ),
            **common,
        )

    if hypothesis.confidence < min_confidence:
        return _deny(
            "low_confidence",
            (
                f"Confidence {hypothesis.confidence} is below the {min_confidence} "
                f"required to act on the running system."
            ),
            **common,
        )

    return PolicyDecision(
        approved=True,
        recommendation="auto_remediate",
        reason=(
            f"{action} on {target} is low blast radius, addresses the diagnosed "
            f"{hypothesis.fault_type} fault, and the investigation concluded at "
            f"confidence {hypothesis.confidence}."
        ),
        **common,
    )


__all__ = ["PolicyDecision", "evaluate"]

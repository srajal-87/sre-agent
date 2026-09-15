"""The policy table: what each action fixes, and how much it can break.

Pure data plus one Pydantic model. The rules that read it live in
``agent/policy/__init__.py``; keeping them apart is what lets the table be
reviewed as a safety artefact on its own.

**Why two tables and not one.** Every fault in this stack lives in process
memory (services/*/app/faults.py), so ``DELETE /admin/fault`` or a container
restart clears *any* of them. Left to blast radius alone, every action would be
a universal fix and the gate would be a rubber stamp. ``ACTION_ADDRESSES`` is
the second, independent rule: an action is only allowed against the mechanism it
is actually for.

Neither table is ever shown to the model. The prompt must not become an answer
key (agent/tests/test_system_prompt.py), so the model proposes from a schema
enum and this table judges the proposal.
"""

from typing import Literal

from pydantic import BaseModel, Field, model_validator

RADIUS_LEVELS = ("low", "medium", "high")

# Which fault mechanisms each action genuinely addresses. Deliberately not the
# full vocabulary anywhere: a restart would *clear* a bad_config fault here, but
# only because the flag happens to sit in process memory, and blessing an action
# for an accidental reason is what this table exists to prevent.
ACTION_ADDRESSES: dict[str, set[str]] = {
    "toggle_config": {"bad_config"},
    "restart_service": {"memory", "latency", "resource_exhaustion"},
    "rollback_deploy": {"timeout", "latency", "bad_config", "memory"},
}

# (action, service) -> level, with "*" as the service wildcard. The restart
# levels follow the dependency chain: api-gateway -> data-service ->
# downstream-dep.
BLAST_RADIUS: dict[tuple[str, str], str] = {
    ("toggle_config", "*"): "low",              # no restart, no dropped requests
    ("restart_service", "downstream-dep"): "low",     # a leaf, nothing depends on it
    ("restart_service", "data-service"): "medium",    # api-gateway depends on it
    ("restart_service", "api-gateway"): "high",       # the entry point; drops all traffic
    ("rollback_deploy", "*"): "high",           # changes what is running
}

# The closed, ordered vocabulary of denial reasons. Ordered because evaluate()
# reports the first rule that fails, and the order decides which reason that is.
#
# Structure before judgement: a proposal is identified and checked for coherence
# before anything is measured. Then blast radius - a property of the action and
# the topology, true on every run - before the confidence of this one run.
#
# AGENT_ALLOW_WRITES is deliberately *not* here. The kill switch is enforced
# once, in run_action, so the gate reaches the same verdict whether or not
# writes are on - which is what makes a dry run across every fault worth
# running. An approved dry run reads honestly: recommendation=auto_remediate,
# and an action result saying executed=False, dry_run=True.
RULES: tuple[str, ...] = (
    "no_action_proposed",
    "unknown_action",
    "unsupported_diagnosis",
    "target_mismatch",
    "action_mismatch",
    "blast_radius",
    "incomplete_run",
    "low_confidence",
)

Recommendation = Literal["auto_remediate", "escalate"]


def blast_radius(action: str, service: str) -> str | None:
    """The level for this pair, or None when the table does not cover it.

    None rather than a level on purpose: an unknown action or service has no
    safe-looking answer, and the caller must deny for the reason that actually
    applies rather than for "not low".
    """
    exact = BLAST_RADIUS.get((action, service))
    if exact is not None:
        return exact
    return BLAST_RADIUS.get((action, "*"))


class PolicyDecision(BaseModel):
    """The gate's verdict. Deterministic, and recorded whether or not it acts."""

    approved: bool
    recommendation: Recommendation
    action: str | None = None
    target: str | None = None
    blast_radius: str | None = None
    # One sentence, for a human reading the report.
    reason: str
    # The machine-readable half, drawn from RULES. Phase 5 scores against it.
    rule: str | None = None

    @model_validator(mode="after")
    def _check_coherence(self) -> "PolicyDecision":
        expected = "auto_remediate" if self.approved else "escalate"
        if self.recommendation != expected:
            raise ValueError(
                f"approved={self.approved} must recommend '{expected}', "
                f"not '{self.recommendation}'"
            )
        if not self.approved and self.rule is None:
            raise ValueError("a denial must name the rule that denied it")
        if self.rule is not None and self.rule not in RULES:
            raise ValueError(f"unknown policy rule '{self.rule}'; known: {list(RULES)}")
        return self

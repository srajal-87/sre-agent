"""The investigation graph's state, and the typed values that live in it.

A ``TypedDict`` for the state itself, Pydantic models for the payloads inside
it. That split is deliberate: LangGraph's ``Annotated[list[X], operator.add]``
reducers are what a TypedDict is for, while a Pydantic *state model* would
re-validate every field on every node return - six times a run, for no benefit.
The payloads stay Pydantic, so ``model_dump(mode="json")`` into the
``investigations.evidence`` jsonb column stays trivial.

Three fields carry a reducer (``evidence``, ``transcript``, ``seen_calls``,
plus ``errors``): a node returns **only the new items** for those, and
LangGraph appends. Every other field is overwritten by whatever a node returns.

``reference_time`` is the single most important field. It is frozen once, at
START, and every window anchors to it - ``query_deploy_history`` measures
``minutes_before_reference`` from it. If each node called ``utc_now()``
independently, two evidence entries from one investigation would describe
different windows and the run would not be reproducible for the Phase 5 eval.
"""

import json
import operator
from datetime import datetime
from typing import Annotated, Callable, Literal, TypedDict
from uuid import UUID

from pydantic import BaseModel, Field, SerializeAsAny, computed_field, field_validator

from agent.policy.table import PolicyDecision, Recommendation
from agent.tools.actions import ACTIONS
from agent.tools.base import ActionResult, ToolResult, utc_now


def action_signature(action: str, target: str) -> str:
    """How an action reads in the ``action_taken`` column.

    Deliberately not ``action_query``'s ``restart_service(service=x)``: that one
    is a machine-checkable citation, this one is read by a person scanning a
    table of investigations.
    """
    return f"{action}({target})"

# Published to the model as a schema enum, so it proposes from the real action
# names rather than inventing one. Typed str all the same - see Hypothesis.
ACTION_NAMES = tuple(sorted(ACTIONS))

# What each action *does*, built from the registry so it cannot drift from the
# code. This is the line the 3.3 rehearsal forced us to draw: an answer key
# (this fault -> that action) must never reach the model, but the semantics of
# an action legitimately must. Asked to propose "the narrowest action that
# addresses the mechanism" from bare names, the model proposed a rollback for a
# configuration fault - a reasonable guess with no information to better it.
# None of these descriptions names a fault type, and a test holds that line.
ACTION_SEMANTICS = "\n".join(
    f"  - {name}: {ACTIONS[name]['description']}" for name in ACTION_NAMES
)

# The closed vocabulary the agent may diagnose in. Closed so that Phase 5 can
# score mechanically against incidents.ground_truth_fault; "unknown" exists so
# the agent can honestly decline rather than pick the nearest label.
FaultType = Literal[
    "timeout",
    "latency",
    "bad_config",
    "memory",
    "resource_exhaustion",
    "unknown",
]


class AlertSummary(BaseModel):
    """A distilled Alertmanager alert - the six fields the model needs.

    The raw webhook is mostly routing metadata. Rendering it verbatim into the
    prompt would spend tokens on ``groupKey`` and ``generatorURL``.
    """

    alertname: str
    service: str | None = None
    severity: str = "unknown"
    summary: str = ""
    description: str = ""
    started_at: datetime | None = None


class Hypothesis(BaseModel):
    """The current best explanation, replaced wholesale each reason cycle.

    ``confidence`` lives here rather than at the top of the state because the
    two always move together: a confidence detached from the claim it scores is
    not interpretable.

    Every field carries a description because this model is also the input
    schema of the ``update_hypothesis`` tool (see ``agent/graph/hypothesis.py``)
    - the schema is where the model reads its instructions for each field, so a
    bare annotation here is a missing instruction there.
    """

    fault_type: FaultType = Field(
        description=(
            "The kind of fault you believe is occurring. Use 'unknown' when the "
            "evidence does not yet support any of the others."
        )
    )
    service: str | None = Field(
        default=None,
        description=(
            "The service where the FAULT is. This is often not the service where "
            "the symptom appears."
        ),
    )
    statement: str = Field(
        description=(
            "One sentence naming the mechanism, not the symptom: what is failing, "
            "and why that produces what the alert reports."
        )
    )
    confidence: float = Field(
        description=(
            "How sure you are that this is the cause, from 0.0 to 1.0, using the "
            "bands in the confidence rubric."
        )
    )
    rationale: str = Field(
        description=(
            "Two to four sentences justifying that confidence, naming the specific "
            "evidence it rests on."
        )
    )
    citations: list[str] = Field(
        default_factory=list,
        description=(
            "The literal query strings, copied verbatim from the tool results you "
            "are relying on. A claim you cannot cite does not raise confidence."
        ),
    )
    ruled_out: list[str] = Field(
        default_factory=list,
        description=(
            "Explanations the evidence excludes, each with the negative signal "
            "that excludes it, e.g. 'code change - no deploys in the window'."
        ),
    )

    proposed_action: str | None = Field(
        default=None,
        description=(
            "Optional. The single action you would take to remediate this, if "
            "any. Propose the narrowest action that addresses the mechanism you "
            "named, and only once you are confident. Your proposal is checked "
            "against a deterministic policy and may be refused.\n"
            "The actions available, and what each one does:\n" + ACTION_SEMANTICS
        ),
        json_schema_extra={"enum": sorted(ACTION_NAMES)},
    )
    action_target: str | None = Field(
        default=None,
        description=(
            "Optional. The service the proposed action would act on. It must be "
            "the service you named above as the location of the fault."
        ),
    )

    @field_validator("citations", "ruled_out", mode="before")
    @classmethod
    def _accept_a_bare_string(cls, value):
        """A forgiving boundary, per the tool-layer convention: a model that
        sends one citation as a string rather than a list of one must not cost a
        whole turn."""
        if isinstance(value, str):
            return [value]
        return value

    @field_validator("confidence")
    @classmethod
    def _clamp_confidence(cls, value: float) -> float:
        """Clamp rather than reject, for the same reason the tools clamp
        ``lookback_minutes``: the model will occasionally send 1.2, and losing a
        whole turn to that is worse than reading it as "as sure as it gets"."""
        return max(0.0, min(1.0, value))


class ToolCall(BaseModel):
    """One requested tool invocation: what to run, and why the model wants it."""

    name: str
    arguments: dict = Field(default_factory=dict)
    why: str = ""
    # The Anthropic tool_use id, so the tool_result block can echo it back.
    id: str | None = None

    def signature(self) -> str:
        """A canonical ``tool:sorted-json-args`` key, for dedupe.

        Key order and the model's stated reason are excluded, so re-asking the
        same question in different words is still caught. Readable rather than
        hashed: these end up in ``seen_calls``, which a human reads when a run
        stops with ``no_new_evidence``.
        """
        return f"{self.name}:{json.dumps(self.arguments, sort_keys=True, default=str)}"


class EvidenceEntry(BaseModel):
    """One tool result, plus when it was gathered and why it was asked for.

    Wraps rather than replaces ``ToolResult`` so ``source`` + ``query`` +
    ``window`` survive intact - that triple is what makes a cited diagnosis
    mechanically checkable.
    """

    iteration: int
    # SerializeAsAny, not a bare ToolResult: Pydantic serialises to the
    # *declared* type by default, which would silently drop MetricsResult.series
    # and LogsResult.message_counts - every number the diagnosis rests on.
    result: SerializeAsAny[ToolResult]
    requested_because: str = ""
    tool_use_id: str | None = None


class AssistantTurn(BaseModel):
    """One assistant turn, kept as raw Anthropic content blocks.

    Raw rather than distilled for one hard reason: with extended thinking on,
    the API requires the ``thinking`` block that preceded a ``tool_use`` to come
    back unmodified, signature included. A reconstructed turn would be rejected.

    The opening sweep's turn is synthesised rather than generated, so that all
    evidence has one uniform representation (see ``graph/messages.py``).
    """

    iteration: int
    content: list[dict] = Field(default_factory=list)


class StepRecord(BaseModel):
    """One reason cycle, for the audit trail.

    ``investigations.steps`` is an int *count*, not a transcript, so this nests
    inside the ``evidence`` jsonb column at persist time.
    """

    iteration: int
    hypothesis: Hypothesis | None = None
    tool_calls: list[ToolCall] = Field(default_factory=list)
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    latency_ms: int = 0
    note: str | None = None

    @computed_field
    @property
    def confidence(self) -> float | None:
        """Serialised, not just computed: the confidence *trajectory* across
        steps is the readable part of the trail."""
        return self.hypothesis.confidence if self.hypothesis else None


class InvestigationReport(BaseModel):
    """What the graph produces. The caller writes it to Postgres.

    The field names line up with the ``investigations`` columns that
    ``POST /investigate`` already opens as a pending stub. Note ``steps``: the
    column is an int *count*, so the step-by-step trail nests inside the
    ``evidence`` jsonb alongside the tool results.
    """

    incident_id: UUID | None = None
    investigation_id: UUID | None = None

    diagnosis: str
    fault_type: FaultType = "unknown"
    service: str | None = None
    confidence: float = 0.0
    citations: list[str] = Field(default_factory=list)
    ruled_out: list[str] = Field(default_factory=list)
    # The policy gate's verdict, never the model's. "escalate" is the resting
    # state: it is what a report says unless every rule in agent/policy/ passed.
    recommendation: Recommendation = "escalate"

    # The gate's full reasoning, kept typed. There is no column for it - it
    # nests into the evidence jsonb - but a denial that cannot be read back is
    # not an audit trail.
    policy_decision: PolicyDecision | None = None
    # These two map to the existing text columns on investigations, so they are
    # rendered strings rather than models: "restart_service(downstream-dep)"
    # and the action's own one-line summary.
    action_taken: str | None = None
    action_result: str | None = None

    evidence: dict = Field(default_factory=dict)
    steps: int = 0

    stop_reason: str | None = None
    status: str = "completed"
    llm_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    latency_ms: int = 0
    notes: list[str] = Field(default_factory=list)
    error: str | None = None


class InvestigationState(TypedDict):
    """Everything the graph carries between nodes."""

    # -- input: set once at START, never mutated --
    incident_id: UUID | None
    investigation_id: UUID | None
    alert: AlertSummary
    reference_time: datetime

    # -- accumulating: nodes return only the NEW items --
    evidence: Annotated[list[EvidenceEntry], operator.add]
    transcript: Annotated[list[StepRecord], operator.add]
    seen_calls: Annotated[list[str], operator.add]
    # What the model actually said, replayed verbatim on the next call.
    assistant_turns: Annotated[list[AssistantTurn], operator.add]
    # LLM-level failures only. A tool returning ok=False is evidence, not an
    # error, and blurring that would undo the whole tool-layer convention.
    errors: Annotated[list[str], operator.add]
    # Things the graph did to the model's output - a capped confidence, a
    # declined call. Not failures, but the report would be misleading without
    # them.
    notes: Annotated[list[str], operator.add]

    # -- current belief: overwritten each cycle --
    hypothesis: Hypothesis | None

    # -- remediation: set once, by act, on the stop path --
    policy_decision: PolicyDecision | None
    action: ActionResult | None

    # -- control --
    iteration: int
    pending_tool_calls: list[ToolCall]
    stop_reason: str | None  # confident | model_finished | max_iterations | ...
    status: str  # running | completed | failed - matches the DB check constraint
    # Reset to 0 by any good turn. `errors` is append-only and cannot tell "two
    # in a row" from "two an hour apart", which is what the llm_error stop
    # condition is actually about.
    consecutive_llm_errors: int

    # -- cost and observability --
    llm_calls: int
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cost_usd: float
    started_at: datetime
    latency_ms: int

    # -- output: set once, by finalize --
    report: InvestigationReport | None


def initial_state(
    alert: AlertSummary,
    *,
    incident_id: UUID | None = None,
    investigation_id: UUID | None = None,
    now: Callable[[], datetime] = utc_now,
) -> InvestigationState:
    """Seed the state for one investigation.

    ``reference_time`` is the alert's start when it has one - an incident from
    five hours ago must be investigated in *its* window, not in the last
    fifteen minutes. ``started_at`` is always the wall clock, because it is
    what ``latency_ms`` is measured against.
    """
    wall_clock = now()
    return InvestigationState(
        incident_id=incident_id,
        investigation_id=investigation_id,
        alert=alert,
        reference_time=alert.started_at or wall_clock,
        evidence=[],
        transcript=[],
        seen_calls=[],
        assistant_turns=[],
        notes=[],
        errors=[],
        hypothesis=None,
        policy_decision=None,
        action=None,
        iteration=0,
        pending_tool_calls=[],
        stop_reason=None,
        status="running",
        consecutive_llm_errors=0,
        llm_calls=0,
        input_tokens=0,
        output_tokens=0,
        cache_read_tokens=0,
        cost_usd=0.0,
        started_at=wall_clock,
        latency_ms=0,
        report=None,
    )

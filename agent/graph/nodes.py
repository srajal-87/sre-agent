"""The graph's nodes.

``gather_evidence`` is an executor, not a decider. It chooses tools exactly
once - the opening sweep - and after that runs what ``reason`` asked for, because
responsibility for "what next" belongs to the node that has the evidence in
front of it.

The invariant everything here serves: **every requested call produces exactly
one evidence entry.** The model's assistant turn is replayed verbatim on the
next request, and the API rejects a ``tool_use`` block with no matching
``tool_result`` - so a call this node declines to run still gets an entry whose
result explains why. Dropping the call would produce a 400 and lose the run.
"""

import asyncio
from datetime import datetime

from agent import config
from agent.graph.hypothesis import UPDATE_HYPOTHESIS_NAME, parse_hypothesis
from agent.graph.llm import call_model, tool_definitions
from agent.graph.messages import build_messages, synthetic_assistant_turn
from agent.graph.state import (
    AlertSummary,
    AssistantTurn,
    EvidenceEntry,
    InvestigationReport,
    InvestigationState,
    StepRecord,
    ToolCall,
    action_signature,
)
from agent.policy import evaluate
from agent.prompts.system import SYSTEM_PROMPT
from agent.tools import run_tool
from agent.tools.actions import run_action as run_action_default
from agent.tools.base import ActionResult, ToolResult, failure, utc_now

EXECUTOR_SOURCE = "agent graph executor"

# The error a declined repeat carries. decide reads it to recognise a turn that
# asked for nothing new.
DUPLICATE_ERROR = "duplicate call"

# Stop reasons that mean the turn came back intact. Anything else is noted;
# only a refusal (or a failed call) is treated as a failed turn.
EXPECTED_STOP_REASONS = {"tool_use", "end_turn", None}

# The model's stated reasoning, kept with each call it asked for. Capped because
# it is stored per evidence entry in the jsonb column.
MAX_STATED_REASON = 500

RETRY_NUDGE = (
    f"You did not call {UPDATE_HYPOTHESIS_NAME}. Call it now with your current "
    f"best explanation and a confidence, even if the evidence is thin and even "
    f"if nothing has changed since your last turn. Re-request any read tools you "
    f"still want alongside it."
)

# Windows for the opening sweep. 30m of metrics is long enough to show where a
# change began; 15m of logs is the container-stdout window that stays cheap;
# 120m of deploys covers a release that shipped well before it broke anything.
SWEEP_METRICS_MINUTES = 30
SWEEP_LOGS_MINUTES = 15
SWEEP_DEPLOYS_MINUTES = 120


def opening_sweep(alert: AlertSummary, reference_time: datetime) -> list[ToolCall]:
    """The four questions an on-call engineer asks in the first sixty seconds.

    Deterministic, so it costs no tokens, and so every investigation starts from
    the same evidence baseline - which is what makes the Phase 5 eval comparable
    across runs. Per the 3.1 rehearsal these four calls already separate three of
    the four fault types; the fourth (a memory fault) produces nothing in logs
    and moves only its own gauge, so the model has to go and find it on turn 2.
    That is the point: it gives the loop real work rather than a rubber stamp.

    Note on windows: only the deploy call is anchored to ``reference_time``,
    because deploy correlation is a question about what shipped *before* the
    incident. The metric and log calls use a lookback from now, which is what
    you want during a live fault - anchoring them to the alert's start would cut
    off the most recent minutes, where an ongoing fault is most visible. The
    cost is that replaying a hours-old incident reads the wrong window; passing
    since/until on those calls is the fix if that ever matters.

    Note on scope: **no call is filtered to the alerting service.** The cause is
    usually downstream of the symptom, and api-gateway's telemetry is provably
    identical under a gateway timeout fault and a downstream-dep latency fault -
    same 504s, same upstream_timeouts_total, same ~2s duration bucket, same log
    line. Nothing measured at the alerting service separates them; the
    discriminators are one and two hops down (a request rate that goes to zero,
    a latency series that stops reporting). Scoping the baseline to the alert
    would withhold exactly the evidence that decides the question.
    """
    return [
        ToolCall(
            name="query_metrics",
            arguments={
                "metric": "http_requests_total",
                "aggregation": "rate",
                "lookback_minutes": SWEEP_METRICS_MINUTES,
            },
            why=(
                "Establish where traffic is still flowing along the chain and "
                "where it is erroring."
            ),
        ),
        ToolCall(
            name="query_metrics",
            arguments={
                "metric": "http_request_duration_seconds",
                "aggregation": "p99",
                "lookback_minutes": SWEEP_METRICS_MINUTES,
            },
            why=(
                "Establish which services have become slow or stopped "
                "reporting, and when."
            ),
        ),
        ToolCall(
            name="query_logs",
            arguments={
                "levels": ["ERROR", "WARNING"],
                "lookback_minutes": SWEEP_LOGS_MINUTES,
            },
            why="Look for errors across all three services, not just the alerting one.",
        ),
        ToolCall(
            name="query_deploy_history",
            arguments={
                "lookback_minutes": SWEEP_DEPLOYS_MINUTES,
                "reference_time": reference_time.isoformat(),
            },
            why="Establish whether a code change could have triggered this.",
        ),
    ]


def _declined(call: ToolCall, *, error: str, summary: str) -> ToolResult:
    """The result for a call the executor chose not to run."""
    return failure(
        tool=call.name,
        source=EXECUTOR_SOURCE,
        query="",
        error=error,
        summary=summary,
    )


def _crashed(call: ToolCall, exc: BaseException) -> ToolResult:
    return failure(
        tool=call.name,
        source=EXECUTOR_SOURCE,
        query="",
        error=f"{type(exc).__name__}: {exc}",
        summary=(
            f"The {call.name} call failed unexpectedly, so that signal is "
            f"unavailable. Try a different tool."
        ),
    )


def _triage(
    calls: list[ToolCall], seen: set[str], *, cap: int | None
) -> tuple[list[ToolCall], dict[str, ToolResult]]:
    """Split the requested calls into those to run and those to decline.

    Declining is not dropping: each declined call still gets a result, because
    its ``tool_use`` block will be replayed and must be answered.
    """
    to_run: list[ToolCall] = []
    declined: dict[str, ToolResult] = {}

    for call in calls:
        signature = call.signature()
        if signature in seen:
            declined[call.id] = _declined(
                call,
                error=DUPLICATE_ERROR,
                summary=(
                    "Not run: this exact query was already issued in this "
                    "investigation, and its result is earlier in this "
                    "conversation. Ask something different."
                ),
            )
            continue
        if cap is not None and len(to_run) >= cap:
            declined[call.id] = _declined(
                call,
                error="over the per-turn call cap",
                summary=(
                    f"Not run: at most {cap} tool calls are executed per turn. "
                    f"Ask for this again on your next turn if you still need it."
                ),
            )
            continue
        # Added to `seen` immediately so a repeat inside one turn is caught too.
        seen.add(signature)
        to_run.append(call)

    return to_run, declined


async def gather_evidence(state: InvestigationState, *, run=run_tool) -> dict:
    """Run the opening sweep, or whatever ``reason`` asked for."""
    iteration = state["iteration"]
    is_sweep = iteration == 0

    if is_sweep:
        calls = opening_sweep(state["alert"], state["reference_time"])
        turn = synthetic_assistant_turn(iteration, calls)
        assistant_turns = [turn]
    else:
        calls = list(state["pending_tool_calls"])
        assistant_turns = []

    if not calls:
        return {"pending_tool_calls": []}

    seen = set(state["seen_calls"])
    before = set(seen)
    # The cap is token discipline for a model that asks for too much. The sweep
    # is four fixed calls chosen by code, so it does not apply.
    to_run, declined = _triage(
        calls, seen, cap=None if is_sweep else config.MAX_TOOL_CALLS_PER_ITERATION
    )

    outcomes = await asyncio.gather(
        *(run(call.name, call.arguments) for call in to_run), return_exceptions=True
    )
    results = {
        call.id: _crashed(call, outcome) if isinstance(outcome, BaseException) else outcome
        for call, outcome in zip(to_run, outcomes)
    }

    evidence = [
        EvidenceEntry(
            iteration=iteration,
            result=results.get(call.id) or declined[call.id],
            requested_because=call.why,
            tool_use_id=call.id,
        )
        # Ordered by what was requested, not by what ran, so the entries line up
        # with the tool_use blocks they answer.
        for call in calls
    ]

    return {
        "evidence": evidence,
        # Only what actually ran: registering a call declined for the cap would
        # make it permanently unaskable.
        "seen_calls": sorted(seen - before),
        "assistant_turns": assistant_turns,
        "pending_tool_calls": [],
    }


# -- reason -----------------------------------------------------------


def _stated_reason(blocks: list[dict]) -> str:
    """The model's own words for why it wants what it asked for.

    Prefers visible text; falls back to the thinking summary. Recorded on each
    evidence entry, which is what makes the trail readable months later.
    """
    for types in (("text",), ("thinking",)):
        said = [
            (block.get("text") or block.get("thinking") or "").strip()
            for block in blocks
            if block.get("type") in types
        ]
        joined = " ".join(part for part in said if part)
        if joined:
            return joined[:MAX_STATED_REASON]
    return ""


def _read_calls(blocks: list[dict]) -> list[ToolCall]:
    """Every tool_use block except the control tool.

    Deliberately not filtered against the registry: a call naming a tool that
    does not exist must still be dispatched, because run_tool turns it into a
    readable ok=False observation *and* the block gets answered. Silently
    dropping it would leave a tool_use with no tool_result, which is a 400.
    """
    why = _stated_reason(blocks)
    return [
        ToolCall(
            name=block.get("name", ""),
            arguments=block.get("input") or {},
            why=why,
            id=block.get("id"),
        )
        for block in blocks
        if block.get("type") == "tool_use" and block.get("name") != UPDATE_HYPOTHESIS_NAME
    ]


def _hypothesis_from(blocks: list[dict]):
    for block in blocks:
        if block.get("type") == "tool_use" and block.get("name") == UPDATE_HYPOTHESIS_NAME:
            return parse_hypothesis(block.get("input"))
    return None


async def reason(state: InvestigationState, *, call=call_model) -> dict:
    """One model call: turn the evidence into a hypothesis and a next move.

    The model is required to call ``update_hypothesis`` every turn. A turn
    without one costs a single retry with an explicit nudge; if that also comes
    back without one, the previous hypothesis is carried forward and a note is
    recorded. Losing the run over a formatting slip would throw away every piece
    of evidence already gathered.

    The retry re-sends the original messages plus the nudge rather than replaying
    the non-compliant turn: that turn's tool_use blocks have no results yet - the
    tools have not run - and replaying them would be rejected.
    """
    iteration = state["iteration"] + 1
    system = SYSTEM_PROMPT
    tools = tool_definitions()
    messages = build_messages(state)

    errors: list[str] = []
    llm_calls = 0
    input_tokens = output_tokens = cache_read = 0
    cost = 0.0
    latency = 0

    response = await call(system=system, tools=tools, messages=messages)
    attempts = [response]

    # Only an otherwise-good turn is worth retrying. A refused or failed call has
    # no hypothesis either, and re-asking would just buy a second failure.
    if (
        response.ok
        and response.stop_reason != "refusal"
        and _hypothesis_from(response.content) is None
    ):
        # One retry, with the omission stated plainly.
        retry = await call(
            system=system,
            tools=tools,
            messages=messages + [{"role": "user", "content": RETRY_NUDGE}],
        )
        attempts.append(retry)
        response = retry

    for attempt in attempts:
        llm_calls += 1
        input_tokens += attempt.input_tokens
        output_tokens += attempt.output_tokens
        cache_read += attempt.cache_read_tokens
        cost += attempt.cost_usd
        latency += attempt.latency_ms

    totals = {
        "iteration": iteration,
        "llm_calls": state["llm_calls"] + llm_calls,
        "input_tokens": state["input_tokens"] + input_tokens,
        "output_tokens": state["output_tokens"] + output_tokens,
        "cache_read_tokens": state["cache_read_tokens"] + cache_read,
        "cost_usd": round(state["cost_usd"] + cost, 6),
    }

    # A refusal is a 200 with no tool calls, which is otherwise indistinguishable
    # from "I am done" - so it is a failed turn, not a finished one.
    if not response.ok or response.stop_reason == "refusal":
        note = response.error or f"model refused: {response.refusal}"
        return {
            **totals,
            "errors": [f"iteration {iteration}: {note}"],
            "consecutive_llm_errors": state["consecutive_llm_errors"] + 1,
            "pending_tool_calls": [],
            "assistant_turns": [],
            "transcript": [
                StepRecord(
                    iteration=iteration,
                    hypothesis=state["hypothesis"],
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    cache_read_tokens=cache_read,
                    latency_ms=latency,
                    note=note,
                )
            ],
        }

    if response.stop_reason not in EXPECTED_STOP_REASONS:
        errors.append(
            f"iteration {iteration}: unexpected stop_reason "
            f"'{response.stop_reason}'; the turn may be incomplete"
        )

    hypothesis = _hypothesis_from(response.content)
    note = None
    if hypothesis is None:
        note = (
            f"the model did not call {UPDATE_HYPOTHESIS_NAME} after a retry; "
            f"carrying the previous hypothesis forward"
        )
        errors.append(f"iteration {iteration}: {note}")
        hypothesis = state["hypothesis"]

    calls = _read_calls(response.content)

    return {
        **totals,
        "hypothesis": hypothesis,
        "pending_tool_calls": calls,
        "assistant_turns": [
            AssistantTurn(iteration=iteration, content=response.content)
        ],
        "transcript": [
            StepRecord(
                iteration=iteration,
                hypothesis=hypothesis,
                tool_calls=calls,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                cache_read_tokens=cache_read,
                latency_ms=latency,
                note=note,
            )
        ],
        "errors": errors,
        "consecutive_llm_errors": 0,
    }


# -- decide -----------------------------------------------------------


def _normalise(text: str) -> str:
    """Collapse whitespace, so a re-wrapped citation still matches.

    Long PromQL comes back from the model with different line breaks than it
    went out with. That is a formatting difference, not a fabricated citation,
    and capping confidence for it would punish the wrong thing.
    """
    return " ".join(text.split())


def _unresolved_citations(state: InvestigationState, hypothesis) -> list[str]:
    """Citations naming a query no tool in this investigation ever issued."""
    issued = {
        _normalise(entry.result.query)
        for entry in state["evidence"]
        if entry.result.query
    }
    return [c for c in hypothesis.citations if _normalise(c) not in issued]


def _corroborating_tools(state: InvestigationState) -> set[str]:
    """Distinct tools that came back ok. A single signal is a symptom."""
    return {entry.result.tool for entry in state["evidence"] if entry.result.ok}


def _only_repeated_itself(state: InvestigationState) -> bool:
    """True when the newest round of evidence was entirely duplicates."""
    if not state["evidence"]:
        return False
    newest = max(entry.iteration for entry in state["evidence"])
    latest = [entry for entry in state["evidence"] if entry.iteration == newest]
    return bool(latest) and all(
        entry.result.error == DUPLICATE_ERROR for entry in latest
    )


def _has_plateaued(state: InvestigationState) -> bool:
    """True when confidence has stopped moving across two consecutive cycles.

    This is the honest version of the brief's "all signals checked". "All three
    tools have been called" is satisfied by the opening sweep before any
    reasoning has happened; this asks the question that was actually meant -
    has more looking stopped changing the answer?
    """
    recent = [step.confidence for step in state["transcript"][-2:]]
    if len(recent) < 2 or any(value is None for value in recent):
        return False
    return abs(recent[-1] - recent[-2]) < config.CONFIDENCE_PLATEAU


def decide(state: InvestigationState, *, now=utc_now) -> dict:
    """Validate the hypothesis, then decide whether the loop goes round again.

    Deterministic by design. The confidence the report carries is not simply
    the number the model wrote down: an uncited claim is capped here, and
    "confident" additionally requires two distinct tools to have returned
    something. That is what makes the score a claim the code will stand behind.
    """
    hypothesis = state["hypothesis"]
    notes: list[str] = []
    update: dict = {}

    if hypothesis is not None:
        unresolved = _unresolved_citations(state, hypothesis)
        # No citations at all is not a free pass: it would be a cheaper route to
        # a high confidence than citing something badly.
        if unresolved or not hypothesis.citations:
            capped = min(hypothesis.confidence, config.UNCITED_CONFIDENCE_CAP)
            if unresolved:
                notes.append(
                    f"iteration {state['iteration']}: confidence capped at {capped} - "
                    f"cited queries that were never issued: {unresolved}"
                )
            else:
                notes.append(
                    f"iteration {state['iteration']}: confidence capped at {capped} - "
                    f"the hypothesis cites no evidence"
                )
            # A copy, not an edit: the transcript already holds this object, and
            # rewriting it would falsify the audit trail after the fact.
            hypothesis = hypothesis.model_copy(update={"confidence": capped})

    # Always returned, capped or not: what decide emits is the *validated*
    # hypothesis, and the report is built from that rather than from whatever
    # the model last said.
    update["hypothesis"] = hypothesis

    elapsed_seconds = (now() - state["started_at"]).total_seconds()
    confidence = hypothesis.confidence if hypothesis else 0.0

    # Ordered so the reported reason is the truest description of why the run
    # ended: a run that was both confident and out of iterations was confident.
    if state["consecutive_llm_errors"] >= config.MAX_CONSECUTIVE_LLM_ERRORS:
        stop_reason, status = "llm_error", "failed"
    elif (
        confidence >= config.CONFIDENCE_THRESHOLD
        and len(_corroborating_tools(state)) >= 2
    ):
        stop_reason, status = "confident", "completed"
    elif not state["pending_tool_calls"] and state["consecutive_llm_errors"] == 0:
        # The model asked for nothing more. Legitimate below the threshold: it
        # escalates with the confidence actually reached, and must not loop
        # hunting for evidence to justify a higher one.
        stop_reason, status = "model_finished", "completed"
    elif state["cost_usd"] > config.COST_CAP_USD:
        stop_reason, status = "budget_exhausted", "completed"
    elif elapsed_seconds > config.WALL_CLOCK_CAP_SECONDS:
        stop_reason, status = "budget_exhausted", "completed"
    elif state["iteration"] >= config.MAX_ITERATIONS:
        stop_reason, status = "max_iterations", "completed"
    elif _only_repeated_itself(state) or _has_plateaued(state):
        stop_reason, status = "no_new_evidence", "completed"
    else:
        stop_reason, status = None, None

    update["stop_reason"] = stop_reason
    update["notes"] = notes
    if stop_reason is not None:
        update["status"] = status
        update["latency_ms"] = int(elapsed_seconds * 1000)
    return update


def route(state: InvestigationState) -> str:
    """The graph's only conditional edge.

    Reads nothing but the stop reason, so that decide is the single authority
    on when the loop ends - including on the failure paths, which route through
    here rather than jumping straight to finalize.
    """
    return "stop" if state["stop_reason"] else "continue"


# -- act --------------------------------------------------------------


async def act(
    state: InvestigationState,
    *,
    run_action=run_action_default,
    timeout_seconds: float | None = None,
) -> dict:
    """Ask the policy gate, and do exactly what it says.

    Deliberately thin. Every judgement here comes from ``agent/policy/`` and
    every effect from the injected executor, so this node contributes no rules
    of its own - which is what lets the gate be reviewed as one artefact.

    It sits on the stop path and therefore runs at most once, which is how "one
    action per investigation" is structural rather than a counter someone has to
    remember to check.

    Nothing an action does can lose the investigation: an executor that raises
    or hangs becomes an ``ok=False`` result and a note, because a completed
    investigation whose report died to a Docker error is the worst outcome
    available.
    """
    if timeout_seconds is None:
        timeout_seconds = config.ACTION_TIMEOUT_SECONDS

    decision = evaluate(
        state["hypothesis"],
        status=state["status"],
        stop_reason=state["stop_reason"],
    )

    if not decision.approved:
        return {
            "policy_decision": decision,
            "notes": [
                f"policy denied {decision.action or 'any action'}"
                f"{' on ' + decision.target if decision.target else ''}: "
                f"{decision.reason} (rule={decision.rule})"
            ],
        }

    name, target = decision.action, decision.target
    query = f"{name}(service={target})"

    try:
        result = await asyncio.wait_for(
            run_action(name, {"service": target}), timeout=timeout_seconds
        )
    except asyncio.TimeoutError:
        result = failure(
            tool=name, source=EXECUTOR_SOURCE, query=query,
            error=f"{name} timed out after {timeout_seconds:.0f}s",
            summary=(
                f"{name} on '{target}' did not finish within {timeout_seconds:.0f}s "
                f"and was abandoned; it may have completed."
            ),
            model=ActionResult,
        )
        result.target = target
    except Exception as exc:  # noqa: BLE001 - an action must not kill the graph
        result = failure(
            tool=name, source=EXECUTOR_SOURCE, query=query,
            error=f"{name} failed: {exc}",
            summary=f"{name} on '{target}' could not be run: {exc}",
            model=ActionResult,
        )
        result.target = target

    return {
        "policy_decision": decision,
        "action": result,
        "notes": [f"policy approved {name} on {target}: {result.summary}"],
    }


# -- finalize ---------------------------------------------------------


UNDIAGNOSED = (
    "No diagnosis was reached. The investigation ended before the evidence "
    "supported an explanation; the evidence gathered is recorded below."
)


def _diagnosis(hypothesis) -> str:
    """The prose a human reads first."""
    if hypothesis is None:
        return UNDIAGNOSED
    return f"{hypothesis.statement.rstrip('.')}. {hypothesis.rationale}".strip()


def finalize(state: InvestigationState, *, now=utc_now) -> dict:
    """Assemble the report. Nothing here touches Postgres.

    The caller persists it, which is what keeps every graph test free of a
    database, Docker, and a network - the same property that lets the tool suite
    run offline.

    Every stop path routes through here, including the failures, so a run that
    times out or loses the API still yields a readable row rather than a null
    one.
    """
    hypothesis = state["hypothesis"]
    errors = state["errors"]
    decision = state["policy_decision"]
    action = state["action"]

    # action_taken is filled only when something actually happened. A dry run, a
    # failed action and a timeout all leave it null while action_result still
    # says what was attempted - that column must never claim an action the
    # system did not take.
    action_taken = (
        action_signature(action.tool, action.target)
        if action is not None and action.executed
        else None
    )

    return {
        "report": InvestigationReport(
            incident_id=state["incident_id"],
            investigation_id=state["investigation_id"],
            trace_id=state["trace_id"],
            diagnosis=_diagnosis(hypothesis),
            fault_type=hypothesis.fault_type if hypothesis else "unknown",
            service=hypothesis.service if hypothesis else None,
            confidence=hypothesis.confidence if hypothesis else 0.0,
            citations=list(hypothesis.citations) if hypothesis else [],
            ruled_out=list(hypothesis.ruled_out) if hypothesis else [],
            recommendation=decision.recommendation if decision else "escalate",
            policy_decision=decision,
            action_taken=action_taken,
            action_result=action.summary if action is not None else None,
            evidence={
                # The alert, so the row stands on its own without a join.
                "alert": state["alert"].model_dump(mode="json"),
                "results": [
                    entry.model_dump(mode="json") for entry in state["evidence"]
                ],
                # steps is an int column, so the trail lives here.
                "transcript": [
                    step.model_dump(mode="json") for step in state["transcript"]
                ],
            },
            steps=len(state["transcript"]),
            stop_reason=state["stop_reason"],
            status=state["status"] if state["status"] != "running" else "completed",
            llm_calls=state["llm_calls"],
            input_tokens=state["input_tokens"],
            output_tokens=state["output_tokens"],
            cost_usd=state["cost_usd"],
            latency_ms=int((now() - state["started_at"]).total_seconds() * 1000),
            notes=list(state["notes"]),
            error="; ".join(errors) if errors else None,
        ),
        "status": state["status"] if state["status"] != "running" else "completed",
        "latency_ms": int((now() - state["started_at"]).total_seconds() * 1000),
    }

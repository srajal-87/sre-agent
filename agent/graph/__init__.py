"""The investigation graph.

    START -> gather_evidence -> reason -> decide -+-> gather_evidence
              (deterministic)   (the one   (pure) |
                                LLM call)         +-> act -> finalize -> END
                                                     (the gate)

Five nodes, **one** conditional edge, one model call per cycle. ``reason`` is
the only node that talks to Claude; ``gather_evidence`` is an executor, not a
decider; ``decide`` is a pure function of state and the only place the loop can
end - including on the failure paths, which route *through* it rather than
jumping to ``finalize``, so there is exactly one place that sets ``stop_reason``.

Every stop path reaches ``act`` and then ``finalize``. Nothing goes to END
directly, so a run that times out or loses the API still produces a readable
report rather than a null row - and every run, successful or not, carries the
policy gate's recorded decision.

``act`` sitting on the stop path is what makes "one action per investigation"
structural: there is no path on which it runs twice.
"""

from datetime import datetime
from typing import Callable
from uuid import UUID, uuid4

from langgraph.graph import END, START, StateGraph

from agent import config
from agent.graph import trace
from agent.graph.llm import call_model
from agent.graph.nodes import act, decide, finalize, gather_evidence, reason, route
from agent.graph.state import (
    AlertSummary,
    InvestigationReport,
    InvestigationState,
    initial_state,
)
from agent.tools import run_tool
from agent.tools.actions import run_action
from agent.tools.base import utc_now

# LangGraph counts every node execution ("super-step"), and one cycle is four of
# them. Set above our own ceiling so that MAX_ITERATIONS always fires first:
# GraphRecursionError would raise out of ainvoke and lose the whole run, whereas
# our stop condition produces a report.
#
# act adds exactly one super-step to the worst case (20 -> 21, against 29): it
# is on the stop path, so it runs once or not at all.
RECURSION_LIMIT = config.MAX_ITERATIONS * 4 + 5


def run_options(tracing: dict | None = None) -> dict:
    """The config one ``ainvoke`` runs under.

    The recursion ceiling is applied last on purpose: tracing may add keys but
    must never be able to move where the loop stops. With nothing to trace this
    is exactly the config the graph ran under before there was a trace at all.
    """
    return {**(tracing or {}), "recursion_limit": RECURSION_LIMIT}


def run_metadata(report: InvestigationReport) -> dict:
    """The labels that make a trace findable in the LangSmith run list.

    Scalars only: metadata is what the run list filters on, not where the
    evidence goes - that is in the spans, and in the Postgres row.
    """
    return {
        "fault_type": report.fault_type,
        "service": report.service,
        "confidence": report.confidence,
        "recommendation": report.recommendation,
        "cost_usd": report.cost_usd,
        "stop_reason": report.stop_reason,
        "status": report.status,
        "steps": report.steps,
    }


def build_graph(
    *,
    run: Callable = run_tool,
    call: Callable = call_model,
    act_on: Callable = run_action,
    now: Callable[[], datetime] = utc_now,
):
    """Compile the graph, with its collaborators injected.

    ``act_on`` is injected for the same reason as ``run`` and ``call``: with a
    real default it needs no wiring in production, and with a fake in the tests
    no suite can restart a container by accident.

    ``now`` is threaded into both ``decide`` and ``finalize`` rather than left
    to their defaults: they measure elapsed time against ``started_at``, and a
    state seeded from one clock but judged by another reports an elapsed time of
    years and trips the wall-clock cap on the first cycle.

    Both tool callables are wrapped for tracing here, once per build rather than
    per call. With tracing off ``traced_tool`` hands back the same object, so
    the executor calls exactly what it was given.
    """
    run = trace.traced_tool(run)
    act_on = trace.traced_tool(act_on)

    async def _gather(state: InvestigationState) -> dict:
        return await gather_evidence(state, run=run)

    async def _reason(state: InvestigationState) -> dict:
        return await reason(state, call=call)

    def _decide(state: InvestigationState) -> dict:
        return decide(state, now=now)

    async def _act(state: InvestigationState) -> dict:
        return await act(state, run_action=act_on)

    def _finalize(state: InvestigationState) -> dict:
        return finalize(state, now=now)

    builder = StateGraph(InvestigationState)
    builder.add_node("gather_evidence", _gather)
    builder.add_node("reason", _reason)
    builder.add_node("decide", _decide)
    builder.add_node("act", _act)
    builder.add_node("finalize", _finalize)

    builder.add_edge(START, "gather_evidence")
    builder.add_edge("gather_evidence", "reason")
    builder.add_edge("reason", "decide")
    builder.add_conditional_edges(
        "decide", route, {"continue": "gather_evidence", "stop": "act"}
    )
    builder.add_edge("act", "finalize")
    builder.add_edge("finalize", END)

    return builder.compile()


async def investigate(
    alert: AlertSummary,
    *,
    incident_id: UUID | None = None,
    investigation_id: UUID | None = None,
    run: Callable = run_tool,
    call: Callable = call_model,
    act_on: Callable = run_action,
    now: Callable[[], datetime] = utc_now,
    trace_config: Callable = trace.run_config,
) -> InvestigationReport:
    """Investigate one alert and return the report.

    The graph does not touch Postgres: the caller persists what comes back.

    The trace id is minted here rather than read back afterwards. ``run_id`` is
    a ``RunnableConfig`` key and becomes the root run's id, so the trace can be
    named in the report even by a run that fails - but only when the run was
    actually traced, because a report must not cite a trace that does not exist.
    """
    trace_id = uuid4()
    tracing = trace_config(
        trace_id=trace_id,
        incident_id=incident_id,
        investigation_id=investigation_id,
    )

    graph = build_graph(run=run, call=call, act_on=act_on, now=now)
    final = await graph.ainvoke(
        initial_state(
            alert,
            incident_id=incident_id,
            investigation_id=investigation_id,
            trace_id=trace_id if tracing else None,
            now=now,
        ),
        run_options(tracing),
    )

    report = final["report"]
    if tracing:
        trace.annotate_run(trace_id, metadata=run_metadata(report))
    return report


__all__ = [
    "build_graph",
    "investigate",
    "run_metadata",
    "run_options",
    "RECURSION_LIMIT",
]

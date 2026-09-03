"""The investigation graph.

    START -> gather_evidence -> reason -> decide -+-> gather_evidence
              (deterministic)   (the one   (pure) |
                                LLM call)         +-> finalize -> END

Four nodes, **one** conditional edge, one model call per cycle. ``reason`` is
the only node that talks to Claude; ``gather_evidence`` is an executor, not a
decider; ``decide`` is a pure function of state and the only place the loop can
end - including on the failure paths, which route *through* it rather than
jumping to ``finalize``, so there is exactly one place that sets ``stop_reason``.

Every stop path reaches ``finalize``. Nothing goes to END directly, so a run
that times out or loses the API still produces a readable report rather than a
null row.
"""

from datetime import datetime
from typing import Callable
from uuid import UUID

from langgraph.graph import END, START, StateGraph

from agent import config
from agent.graph.llm import call_model
from agent.graph.nodes import decide, finalize, gather_evidence, reason, route
from agent.graph.state import (
    AlertSummary,
    InvestigationReport,
    InvestigationState,
    initial_state,
)
from agent.tools import run_tool
from agent.tools.base import utc_now

# LangGraph counts every node execution ("super-step"), and one cycle is four of
# them. Set above our own ceiling so that MAX_ITERATIONS always fires first:
# GraphRecursionError would raise out of ainvoke and lose the whole run, whereas
# our stop condition produces a report.
RECURSION_LIMIT = config.MAX_ITERATIONS * 4 + 5


def build_graph(
    *,
    run: Callable = run_tool,
    call: Callable = call_model,
    now: Callable[[], datetime] = utc_now,
):
    """Compile the graph, with its collaborators injected.

    ``now`` is threaded into both ``decide`` and ``finalize`` rather than left
    to their defaults: they measure elapsed time against ``started_at``, and a
    state seeded from one clock but judged by another reports an elapsed time of
    years and trips the wall-clock cap on the first cycle.
    """

    async def _gather(state: InvestigationState) -> dict:
        return await gather_evidence(state, run=run)

    async def _reason(state: InvestigationState) -> dict:
        return await reason(state, call=call)

    def _decide(state: InvestigationState) -> dict:
        return decide(state, now=now)

    def _finalize(state: InvestigationState) -> dict:
        return finalize(state, now=now)

    builder = StateGraph(InvestigationState)
    builder.add_node("gather_evidence", _gather)
    builder.add_node("reason", _reason)
    builder.add_node("decide", _decide)
    builder.add_node("finalize", _finalize)

    builder.add_edge(START, "gather_evidence")
    builder.add_edge("gather_evidence", "reason")
    builder.add_edge("reason", "decide")
    builder.add_conditional_edges(
        "decide", route, {"continue": "gather_evidence", "stop": "finalize"}
    )
    builder.add_edge("finalize", END)

    return builder.compile()


async def investigate(
    alert: AlertSummary,
    *,
    incident_id: UUID | None = None,
    investigation_id: UUID | None = None,
    run: Callable = run_tool,
    call: Callable = call_model,
    now: Callable[[], datetime] = utc_now,
) -> InvestigationReport:
    """Investigate one alert and return the report.

    The graph does not touch Postgres: the caller persists what comes back.
    """
    graph = build_graph(run=run, call=call, now=now)
    final = await graph.ainvoke(
        initial_state(
            alert,
            incident_id=incident_id,
            investigation_id=investigation_id,
            now=now,
        ),
        {"recursion_limit": RECURSION_LIMIT},
    )
    return final["report"]


__all__ = ["build_graph", "investigate", "RECURSION_LIMIT"]

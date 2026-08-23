"""Agent tools: the read-only investigation surface.

``TOOLS`` maps a tool name to everything a caller needs: a description written
for the model, the Pydantic input model, the result model, and the async runner.

``input_model.model_json_schema()`` is already the shape the Anthropic tool-use
API wants, so the reasoning loop can register these with no adapter layer - which
is the point of the Pydantic-first convention in CLAUDE.md.
"""

from pydantic import ValidationError

from agent.tools.base import ToolResult, failure
from agent.tools.deploys import DeployQuery, DeployResult, query_deploy_history
from agent.tools.logs import LogsQuery, LogsResult, query_logs
from agent.tools.metrics import MetricsQuery, MetricsResult, query_metrics

REGISTRY_SOURCE = "agent tool registry"

TOOLS = {
    "query_metrics": {
        "description": (
            "Read a Prometheus time series for one of the six metrics this stack "
            "exposes. Name a metric plus optional service/path/status filters and "
            "an aggregation; the tool builds and returns the PromQL. Use it to "
            "establish when a symptom started and how large it is. An empty "
            "result means nothing matched those filters, which is itself evidence."
        ),
        "input_model": MetricsQuery,
        "result_model": MetricsResult,
        "runner": query_metrics,
    },
    "query_logs": {
        "description": (
            "Read structured JSON logs from the victim services' container "
            "stdout, filtered by service, level, message substring, or trace_id. "
            "Returns counts over the full match set plus the most recent lines. "
            "Filter by trace_id to follow a single request across all three "
            "services - that is how a symptom is linked to its cause."
        ),
        "input_model": LogsQuery,
        "result_model": LogsResult,
        "runner": query_logs,
    },
    "query_deploy_history": {
        "description": (
            "List deploys recorded in the ledger for a time window, each with the "
            "number of minutes it shipped before the incident. Use it to decide "
            "whether a code change could have triggered the symptom. No deploys "
            "in the window excludes a code change; an empty ledger does not."
        ),
        "input_model": DeployQuery,
        "result_model": DeployResult,
        "runner": query_deploy_history,
    },
}


def get_tool(name: str) -> dict:
    """Return the registry entry for ``name``, or raise KeyError naming the rest."""
    try:
        return TOOLS[name]
    except KeyError:
        raise KeyError(
            f"unknown tool '{name}'; known: {sorted(TOOLS)}"
        ) from None


async def run_tool(name: str, arguments: dict, **kwargs) -> ToolResult:
    """Validate raw arguments into the tool's input model and run it.

    Both an unknown tool name and malformed arguments come back as ``ok=False``
    observations rather than exceptions: the model *will* send
    ``{"lookback_minutes": "soon"}``, and that has to be something it can read
    and correct, not something that kills the graph.
    """
    entry = TOOLS.get(name)
    if entry is None:
        return failure(
            tool=name,
            source=REGISTRY_SOURCE,
            query="",
            error=f"unknown tool '{name}'; known: {sorted(TOOLS)}",
            summary=f"There is no tool called '{name}'. Available: {', '.join(sorted(TOOLS))}.",
        )

    try:
        query = entry["input_model"](**arguments)
    except ValidationError as exc:
        return failure(
            tool=name,
            source=REGISTRY_SOURCE,
            query="",
            error=f"invalid arguments for {name}: {exc}",
            summary=(
                f"The arguments for {name} were not valid; check the field names "
                f"and types and try again."
            ),
            model=entry["result_model"],
        )

    return await entry["runner"](query, **kwargs)


__all__ = [
    "TOOLS",
    "get_tool",
    "run_tool",
    "ToolResult",
    "MetricsQuery",
    "MetricsResult",
    "query_metrics",
    "LogsQuery",
    "LogsResult",
    "query_logs",
    "DeployQuery",
    "DeployResult",
    "query_deploy_history",
]

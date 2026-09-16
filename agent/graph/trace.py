"""Tracing one investigation to LangSmith.

The trace id is **ours**. ``run_id`` is a valid ``RunnableConfig`` key and it
becomes the root run's id, so a UUID minted before ``ainvoke`` is called is the
id the trace will have - known in advance, writable to Postgres whatever the run
then does, and needing nothing from langsmith's run-tree internals.

Tracing is attached, not ambient. The tracer is built here and passed as a
callback rather than left to ``LANGSMITH_TRACING`` in the environment, so a run
is traced because this code asked for it and lands in the project
``agent.config`` names rather than in langsmith's "default".

**Every function here is fail-open**, which is the tool layer's never-raises
rule applied to observability. A missing package, an unusable key or a dead
network degrades to "not traced" and a run that completes normally. An
investigation must never be lost to the thing watching it.
"""

import os
from types import SimpleNamespace
from typing import Callable
from uuid import UUID

from agent import config

# Either spelling authenticates; langsmith itself reads both.
_API_KEY_VARS = ("LANGSMITH_API_KEY", "LANGCHAIN_API_KEY")

# How long flush() waits for the queue to drain. Long enough for a run's worth
# of spans on a slow link, short enough that a wedged endpoint cannot hold a CLI
# process open - the trace is worth waiting for, but not indefinitely.
FLUSH_TIMEOUT_SECONDS = 10.0

# One client per process, shared by the tracer and by flush(): flushing a
# different client than the one that queued the spans would drain nothing.
_client = None


def tracing_enabled() -> bool:
    """Whether this process should trace.

    A key is part of the question, not a detail. Switched on but
    unauthenticated means every run pays the tracer's overhead and posts
    nothing, so that reads as off.
    """
    if not config.LANGSMITH_TRACING:
        return False
    return any((os.getenv(name) or "").strip() for name in _API_KEY_VARS)


def _langsmith_client():
    """The process-wide langsmith client, created on first use.

    Imported lazily, like the Bedrock client in ``agent/graph/llm.py``: an
    offline test run must never construct one.
    """
    global _client
    if _client is None:
        from langsmith import Client

        _client = Client()
    return _client


def _build_tracer(*, project: str):
    """The real collaborator. Raises; ``run_config`` is what converts."""
    from langchain_core.tracers.langchain import LangChainTracer

    return LangChainTracer(project_name=project, client=_langsmith_client())


def run_config(
    *,
    trace_id: UUID,
    incident_id: UUID | None = None,
    investigation_id: UUID | None = None,
    metadata: dict | None = None,
    build_tracer: Callable = _build_tracer,
) -> dict:
    """The ``RunnableConfig`` fragment that traces one investigation.

    Returns ``{}`` when tracing is off or unavailable - an empty dict rather
    than a disabled one, so merging it into the graph's existing config leaves
    the untraced path exactly as it was.
    """
    if not tracing_enabled():
        return {}

    try:
        tracer = build_tracer(project=config.LANGSMITH_PROJECT)
    except Exception:  # noqa: BLE001 - degrades, does not raise
        return {}

    tags = []
    if incident_id is not None:
        tags.append(f"incident:{incident_id}")
    if investigation_id is not None:
        tags.append(f"investigation:{investigation_id}")

    return {
        "run_id": trace_id,
        "run_name": "investigation",
        "tags": tags,
        "metadata": {
            "incident_id": str(incident_id) if incident_id else None,
            "investigation_id": (
                str(investigation_id) if investigation_id else None
            ),
            **(metadata or {}),
        },
        "callbacks": [tracer],
    }


def _traceable(**kwargs):
    """The real collaborator. Raises; ``traced_tool`` is what converts."""
    from langsmith import traceable

    return traceable(**kwargs)


def traced_tool(run: Callable, *, traceable: Callable = _traceable) -> Callable:
    """Wrap a tool callable so each call is its own span, named for the tool.

    Without this every span in the trace reads ``run_tool`` and the shape of the
    investigation - which tools ran, in what order, how long each took - is
    unreadable. The name has to come from the call rather than the decoration,
    because one callable serves every tool.

    Returns the *same object* when tracing is off, so the offline path is not
    merely equivalent but identical.

    Only the wrapping is guarded. If the span machinery raises mid-call the
    exception travels like any other: ``gather_evidence`` collects it with
    ``return_exceptions=True`` and records a crashed call, which is a truer
    report than a result invented to cover for the tracer.
    """
    if not tracing_enabled():
        return run

    try:
        traced = traceable(run_type="tool")(run)
    except Exception:  # noqa: BLE001 - degrades, does not raise
        return run

    async def _one_call(name, arguments, **kwargs):
        return await traced(
            name, arguments, langsmith_extra={"name": name}, **kwargs
        )

    return _one_call


async def _no_completions(*args, **kwargs):
    """The stand-in's body. It exists to be patched, never to be called."""
    raise NotImplementedError(
        "this client has no completions endpoint; use messages.create"
    )


def _wrap_anthropic(client):
    """The real collaborator. Raises; ``traced_client`` is what converts."""
    from langsmith.wrappers import wrap_anthropic

    return wrap_anthropic(client)


def traced_client(client, *, wrap: Callable = _wrap_anthropic):
    """Wrap a model client so each call is its own span.

    This is what makes a trace worth reading: LangGraph's node spans serialise
    the whole state, so the evidence blob dominates them, while the wrapper
    records the prompt, the tool schemas and the token usage of one call.

    The spans nest correctly without any environment variable. LangGraph looks
    through the run manager's handlers for a LangChainTracer - the one
    ``run_config`` attaches - and sets it as langsmith's parent run tree for the
    duration of the node, so a call made inside a node is mid-trace by
    definition.

    Two hazards, both handled:

    * ``wrap_anthropic`` patches ``messages.create``, ``messages.stream``, then
      reads ``completions.create`` with no guard. ``AsyncAnthropicBedrock`` has
      no ``completions``, so it raises *after* half-patching. A stand-in that
      refuses to be called lets it finish.
    * It mutates in place, so a failure part-way cannot be undone. The except
      path returns the half-patched client, which still works - losing the
      investigation to a failed wrapping would be the worse trade.
    """
    if not tracing_enabled():
        return client

    try:
        if not hasattr(client, "completions"):
            client.completions = SimpleNamespace(create=_no_completions)
        return wrap(client)
    except Exception:  # noqa: BLE001 - degrades, does not raise
        return client


def annotate_run(trace_id: UUID, *, metadata: dict, client=None) -> bool:
    """Label the root run with what the investigation concluded.

    LangSmith's run list shows names and metadata, not contents, so without
    this, finding "the runs that blamed the wrong service" means opening every
    trace by hand. Metadata is for filtering: scalars only, never the evidence.

    Best effort. The run may not have been posted yet, or at all, and losing a
    label is not worth losing the report that goes with it.
    """
    if not tracing_enabled():
        return False
    try:
        (client or _langsmith_client()).update_run(
            trace_id, extra={"metadata": metadata}
        )
        return True
    except Exception:  # noqa: BLE001 - degrades, does not raise
        return False


def flush(*, client=None, timeout: float = FLUSH_TIMEOUT_SECONDS) -> bool:
    """Drain the queued spans. True if anything was waited on.

    Spans are posted from a background thread, so a short-lived process - the
    CLI, a container running one investigation - exits with the queue still
    full and loses the trace it just paid for.
    """
    target = client or _client
    if target is None:
        return False
    try:
        target.flush(timeout=timeout)
        return True
    except Exception:  # noqa: BLE001 - degrades, does not raise
        return False


__all__ = [
    "annotate_run",
    "flush",
    "run_config",
    "traced_client",
    "traced_tool",
    "tracing_enabled",
]

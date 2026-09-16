"""The audit trail is optional, and every failure inside it is silent by design.

These tests never import langsmith. The one place tracing reaches a library is
``_build_tracer``, which ``run_config`` takes as an injected collaborator - the
same convention the tool layer uses for HTTP and Docker, and what keeps this
suite offline.
"""

import asyncio
import os
from contextlib import contextmanager
from types import SimpleNamespace
from uuid import uuid4

from agent import config
from agent.graph import trace

INCIDENT = uuid4()
INVESTIGATION = uuid4()
TRACE = uuid4()


@contextmanager
def _tracing(on: bool, *, api_key: str | None = "ls-test-key"):
    """Set the switch and the key, then put both back.

    The suite restores rather than reloads: ``trace`` reads
    ``config.LANGSMITH_TRACING`` through the module, so a reload would hand it a
    different module object than the one the test just set.
    """
    saved_flag = config.LANGSMITH_TRACING
    keys = ("LANGSMITH_API_KEY", "LANGCHAIN_API_KEY")
    saved_env = {k: os.environ.get(k) for k in keys}
    try:
        config.LANGSMITH_TRACING = on
        for k in keys:
            os.environ.pop(k, None)
        if api_key:
            os.environ["LANGSMITH_API_KEY"] = api_key
        yield
    finally:
        config.LANGSMITH_TRACING = saved_flag
        for k, v in saved_env.items():
            os.environ.pop(k, None)
            if v is not None:
                os.environ[k] = v


class _FakeTracer:
    """Stands in for LangChainTracer; only its identity is asserted on."""

    def __init__(self, project: str):
        self.project = project


def _fake_builder(*, project: str) -> _FakeTracer:
    return _FakeTracer(project)


def _raising_builder(*, project: str):
    raise RuntimeError("No module named 'langsmith'")


def _config(**kwargs) -> dict:
    return trace.run_config(
        trace_id=TRACE,
        incident_id=INCIDENT,
        investigation_id=INVESTIGATION,
        build_tracer=_fake_builder,
        **kwargs,
    )


# ── the switch ───────────────────────────────────────────────────────

def test_tracing_is_off_when_the_switch_is_off():
    with _tracing(False):
        assert trace.tracing_enabled() is False


def test_tracing_is_off_without_an_api_key():
    """Switched on but unauthenticated is the worst case: every run would pay
    the tracer's overhead and post nothing. Treat it as off."""
    with _tracing(True, api_key=None):
        assert trace.tracing_enabled() is False


def test_tracing_is_on_with_the_switch_and_a_key():
    with _tracing(True):
        assert trace.tracing_enabled() is True


def test_the_older_api_key_name_also_counts():
    """LANGCHAIN_API_KEY is what an existing .env is likely to hold."""
    with _tracing(True, api_key=None):
        os.environ["LANGCHAIN_API_KEY"] = "ls-legacy-key"
        assert trace.tracing_enabled() is True


# ── the run config ───────────────────────────────────────────────────

def test_the_run_config_is_empty_when_tracing_is_off():
    """Empty, not "disabled": the caller merges this into ainvoke's config, so
    an empty dict is what makes the default path byte-identical to today's."""
    with _tracing(False):
        assert _config() == {}


def test_the_trace_id_becomes_the_root_run_id():
    """The whole point of minting it ourselves - the id is known before the run
    starts, so it can be written to Postgres whatever the run does."""
    with _tracing(True):
        assert _config()["run_id"] == TRACE


def test_the_run_is_tagged_with_the_incident():
    with _tracing(True):
        assert f"incident:{INCIDENT}" in _config()["tags"]


def test_the_run_is_tagged_with_the_investigation():
    """One incident can be investigated more than once; the tags have to tell
    those runs apart in the LangSmith run list."""
    with _tracing(True):
        assert f"investigation:{INVESTIGATION}" in _config()["tags"]


def test_ids_that_were_never_supplied_produce_no_tags():
    """A CLI run has neither id. An "incident:None" tag would be a filter that
    silently matches every ad-hoc run."""
    with _tracing(True):
        cfg = trace.run_config(trace_id=TRACE, build_tracer=_fake_builder)
        assert cfg["tags"] == []


def test_the_metadata_carries_the_ids_as_strings():
    """jsonb on one side, LangSmith metadata on the other; UUID objects do not
    survive either."""
    with _tracing(True):
        meta = _config()["metadata"]
        assert meta["incident_id"] == str(INCIDENT)
        assert meta["investigation_id"] == str(INVESTIGATION)


def test_extra_metadata_is_merged_in():
    with _tracing(True):
        meta = _config(metadata={"fault_type": "timeout"})["metadata"]
        assert meta["fault_type"] == "timeout"
        assert meta["incident_id"] == str(INCIDENT)


def test_the_tracer_is_attached_as_a_callback_on_the_configured_project():
    """Explicit over ambient: attaching the tracer means the run is traced
    because this code asked for it, not because an env var happened to be set."""
    with _tracing(True):
        callbacks = _config()["callbacks"]
        assert len(callbacks) == 1
        assert callbacks[0].project == config.LANGSMITH_PROJECT


def test_a_tracer_that_cannot_be_built_degrades_to_no_tracing():
    """A missing package, a bad key, a dead network. Observability must not be
    able to take down an investigation."""
    with _tracing(True):
        assert trace.run_config(
            trace_id=TRACE, build_tracer=_raising_builder
        ) == {}


# ── wrapping the model client ────────────────────────────────────────

def _client() -> SimpleNamespace:
    """An Anthropic client with the surface AsyncAnthropicBedrock has.

    Note what is missing: ``completions``. The Bedrock client has no text
    completions endpoint, and that absence is the whole bug this wrapping works
    around.
    """
    return SimpleNamespace(
        messages=SimpleNamespace(create="create", stream="stream")
    )


def _wrap_like_langsmith(client):
    """Mimics wrap_anthropic's real order, which is what makes it fail.

    It patches messages.create, then messages.stream, then completions.create -
    reading the last one straight off the client with no hasattr guard, so a
    client without it raises AttributeError having already been half-patched.
    """
    client.messages.create = f"traced({client.messages.create})"
    client.messages.stream = f"traced({client.messages.stream})"
    client.completions.create = f"traced({client.completions.create})"
    return client


def test_an_untraced_process_gets_its_client_back_untouched():
    """Identity, not equality: with tracing off nothing about the model call
    path may change, down to the object."""
    client = _client()
    with _tracing(False):
        assert trace.traced_client(client, wrap=_wrap_like_langsmith) is client


def test_a_client_with_no_completions_endpoint_is_still_wrapped():
    """The Bedrock client has no .completions, and wrap_anthropic reads it
    unguarded - so without a stand-in the wrapping dies halfway through."""
    with _tracing(True):
        client = trace.traced_client(_client(), wrap=_wrap_like_langsmith)

    assert client.messages.create == "traced(create)"
    assert client.messages.stream == "traced(stream)"


def test_the_completions_stand_in_refuses_to_be_called():
    """It exists to be patched, never to be used. Bedrock has no such endpoint,
    so answering a call would be a lie."""
    client = _client()
    with _tracing(True):
        trace.traced_client(client, wrap=lambda c: c)

    try:
        asyncio.run(client.completions.create())
    except NotImplementedError as exc:
        assert "completions" in str(exc)
    else:
        raise AssertionError("expected a NotImplementedError")


def test_a_wrapper_that_raises_leaves_a_usable_client():
    """wrap_anthropic mutates in place, so the except path cannot hand back a
    pristine client - only a half-patched one that still works. Losing the
    investigation instead would be the worse trade."""
    def _explodes_halfway(client):
        client.messages.create = f"traced({client.messages.create})"
        raise AttributeError("'AsyncAnthropicBedrock' object has no attribute 'x'")

    with _tracing(True):
        client = trace.traced_client(_client(), wrap=_explodes_halfway)

    assert client.messages.create == "traced(create)"
    assert client.messages.stream == "stream"


# ── wrapping a tool call ─────────────────────────────────────────────

class _Runner:
    """Stands in for run_tool / run_action."""

    def __init__(self):
        self.calls = []

    async def __call__(self, name, arguments, **kwargs):
        self.calls.append((name, arguments))
        return f"result of {name}"


class _FakeTraceable:
    """Stands in for langsmith.traceable: records, then delegates."""

    def __init__(self):
        self.decorated_with = None
        self.named = []

    def __call__(self, **kwargs):
        self.decorated_with = kwargs

        def _decorate(func):
            async def _wrapper(*args, langsmith_extra=None, **kw):
                self.named.append((langsmith_extra or {}).get("name"))
                return await func(*args, **kw)

            return _wrapper

        return _decorate


def test_an_untraced_tool_callable_is_handed_back_untouched():
    """Identity: with tracing off the executor calls exactly what it was given,
    so nothing about the offline path moves."""
    runner = _Runner()
    with _tracing(False):
        assert trace.traced_tool(runner, traceable=_FakeTraceable()) is runner


def test_arguments_and_results_pass_through_the_wrapper_unchanged():
    runner = _Runner()
    with _tracing(True):
        traced = trace.traced_tool(runner, traceable=_FakeTraceable())
        result = asyncio.run(traced("query_logs", {"lookback_minutes": 5}))

    assert runner.calls == [("query_logs", {"lookback_minutes": 5})]
    assert result == "result of query_logs"


def test_each_span_is_named_for_the_tool_that_ran():
    """Otherwise every span in the trace reads "run_tool" and the shape of the
    investigation is unreadable."""
    traceable = _FakeTraceable()
    with _tracing(True):
        traced = trace.traced_tool(_Runner(), traceable=traceable)
        asyncio.run(traced("query_metrics", {}))
        asyncio.run(traced("query_deploy_history", {}))

    assert traceable.named == ["query_metrics", "query_deploy_history"]
    assert traceable.decorated_with == {"run_type": "tool"}


def test_a_wrapper_that_cannot_be_built_leaves_the_tool_callable_alone():
    def _explodes(**kwargs):
        raise RuntimeError("langsmith is unusable")

    runner = _Runner()
    with _tracing(True):
        assert trace.traced_tool(runner, traceable=_explodes) is runner


# ── flush and annotate ───────────────────────────────────────────────

class _FakeClient:
    def __init__(self, explode: bool = False):
        self.flushed = 0
        self.updated = []
        self.explode = explode

    def flush(self, timeout=None):
        self.flushed += 1
        if self.explode:
            raise RuntimeError("connection reset")

    def update_run(self, run_id, **kwargs):
        self.updated.append((run_id, kwargs))
        if self.explode:
            raise RuntimeError("404 not found")


def test_flush_drains_the_client():
    """A short-lived CLI process exits with the background queue undrained,
    which loses the trace it just paid for."""
    client = _FakeClient()
    assert trace.flush(client=client) is True
    assert client.flushed == 1


def test_flush_survives_a_client_that_raises():
    assert trace.flush(client=_FakeClient(explode=True)) is False


def test_flush_with_nothing_to_drain_is_a_no_op():
    """Nothing was traced, so there is no client and nothing to wait for."""
    saved = trace._client
    try:
        trace._client = None
        assert trace.flush() is False
    finally:
        trace._client = saved


def test_the_root_run_is_annotated_with_what_the_investigation_concluded():
    """So the run list can be filtered and read without opening every trace."""
    client = _FakeClient()
    with _tracing(True):
        assert trace.annotate_run(
            TRACE, metadata={"fault_type": "timeout"}, client=client
        ) is True

    run_id, kwargs = client.updated[0]
    assert run_id == TRACE
    assert kwargs["extra"]["metadata"]["fault_type"] == "timeout"


def test_annotating_survives_a_run_that_cannot_be_updated():
    """The run may not have been posted yet, or at all. Losing the annotation
    is not worth losing the report."""
    with _tracing(True):
        assert trace.annotate_run(
            TRACE, metadata={"fault_type": "timeout"}, client=_FakeClient(explode=True)
        ) is False


def test_an_untraced_run_is_not_annotated():
    """There is no run to annotate, and asking would build a client."""
    client = _FakeClient()
    with _tracing(False):
        assert trace.annotate_run(TRACE, metadata={}, client=client) is False

    assert client.updated == []

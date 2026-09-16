"""One model call, behind an injectable seam, priced and accounted for.

``call_model`` is the only place the agent talks to a model - Claude on Amazon
Bedrock, authenticated by the ``AWS_BEARER_TOKEN_BEDROCK`` bearer token. It
takes a ``client`` keyword with a real default, matching the tool layer's
convention - so the whole graph is testable with a fake object and no
credentials, no network, and no mocking library.

Like a tool, it **never raises**. An exception thirty seconds into an
investigation would lose the run and every piece of evidence already gathered,
so a failed call comes back as ``ok=False`` with a readable error and the graph
decides what to do about it.

Two API details worth stating, because both are silent failures:

* **Thinking is left on.** With it disabled the model can write a tool call into
  visible text, where the turn succeeds and the call simply never runs. Sonnet
  4.5 predates adaptive thinking, so it takes the explicit form -
  ``{"type": "enabled", "budget_tokens": N}`` - rather than ``"adaptive"``. For
  the same reason no ``output_config`` is sent at all: ``effort`` is a 400 on
  this model.
* **One cache breakpoint, on the last tool definition.** Caching is a prefix
  match over tools -> system -> messages, so that single breakpoint covers the
  whole stable prefix. Tool order is therefore part of the cache key, which is
  why it is sorted rather than left to dict ordering.
"""

import asyncio
import time

from pydantic import BaseModel, Field

from agent import config
from agent.graph.hypothesis import UPDATE_HYPOTHESIS
from agent.tools import TOOLS

# Dollars per million tokens, from the model's published rates. Keyed by the
# literal model string that gets sent, so the Bedrock ids need their own entries
# - without them every call falls through to DEFAULT_PRICING (Opus rates) and
# overstates spend, tripping COST_CAP_USD early.
PRICING = {
    "eu.anthropic.claude-sonnet-4-5-20250929-v1:0": {"input": 3.0, "output": 15.0},
    "anthropic.claude-sonnet-4-5-20250929-v1:0": {"input": 3.0, "output": 15.0},
    "claude-opus-5": {"input": 5.0, "output": 25.0},
    "claude-sonnet-5": {"input": 3.0, "output": 15.0},
    "claude-haiku-4-5": {"input": 1.0, "output": 5.0},
}
# An unpriced model must not report $0.00: that would silently disable the
# budget stop condition. Opus rates are the conservative guess.
DEFAULT_PRICING = PRICING["claude-opus-5"]

# Multipliers on the input rate for cached tokens.
CACHE_WRITE_MULTIPLIER = 1.25
CACHE_READ_MULTIPLIER = 0.1


def tool_definitions(*, cache: bool = True) -> list[dict]:
    """The four tools the model may call, in a stable order.

    The three read tools come from the registry - ``model_json_schema()`` is
    already the shape the API wants - plus ``update_hypothesis``, which the
    graph owns rather than the registry (see agent/graph/hypothesis.py).
    """
    tools = [
        {
            "name": name,
            "description": TOOLS[name]["description"],
            "input_schema": TOOLS[name]["input_model"].model_json_schema(),
        }
        for name in sorted(TOOLS)
    ]
    tools.append(dict(UPDATE_HYPOTHESIS))

    if cache:
        tools[-1] = {**tools[-1], "cache_control": {"type": "ephemeral"}}
    return tools


def estimate_cost(
    model: str,
    input_tokens: int,
    output_tokens: int,
    cache_read_tokens: int = 0,
    cache_creation_tokens: int = 0,
) -> float:
    """Price one call in dollars.

    ``usage.input_tokens`` counts only the uncached tokens, so the three input
    figures are added rather than overlapping.
    """
    rates = PRICING.get(model, DEFAULT_PRICING)
    billable_input = (
        input_tokens
        + cache_creation_tokens * CACHE_WRITE_MULTIPLIER
        + cache_read_tokens * CACHE_READ_MULTIPLIER
    )
    return (billable_input * rates["input"] + output_tokens * rates["output"]) / 1e6


class ModelResponse(BaseModel):
    """What one call produced, whether or not it worked."""

    ok: bool = True
    content: list[dict] = Field(default_factory=list)
    stop_reason: str | None = None
    refusal: str | None = None  # set only when stop_reason == "refusal"
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_creation_tokens: int = 0
    cost_usd: float = 0.0
    latency_ms: int = 0
    error: str | None = None


# Keyed by event loop, for the same reason agent/tools/deploys.py keys its
# sessionmaker that way: the client holds a connection pool bound to the loop
# that opened it, and the CLI runs one asyncio.run() per invocation.
_clients: dict[object, object] = {}


def get_client():
    """Return the async Bedrock client for the running loop.

    Imported lazily so that ``import agent.graph.llm`` works with no AWS
    credentials - which is what keeps the unit tests offline. The region is
    passed explicitly rather than inferred; the bearer token is left to the SDK,
    which reads ``AWS_BEARER_TOKEN_BEDROCK`` from the environment.

    The client is wrapped for tracing once, here, rather than per call: the
    wrapper mutates it in place, so wrapping the cached client twice would nest
    every call in a span inside a span. With tracing off this returns the very
    same object, so nothing about the default path changes.
    """
    from anthropic import AsyncAnthropicBedrock

    from agent.graph.trace import traced_client

    try:
        key = asyncio.get_running_loop()
    except RuntimeError:
        key = None

    if key not in _clients:
        _clients[key] = traced_client(
            AsyncAnthropicBedrock(aws_region=config.BEDROCK_REGION)
        )
    return _clients[key]


def _as_block(block) -> dict:
    """Normalise a content block to a plain dict.

    The graph stores these in state, replays them on the next call, and dumps
    them into the evidence jsonb column, so an SDK object would not survive the
    round trip. ``to_dict()`` keeps only the fields the API actually set.
    """
    if isinstance(block, dict):
        return block
    to_dict = getattr(block, "to_dict", None)
    if callable(to_dict):
        return to_dict()
    return block.model_dump(exclude_none=True)


async def call_model(
    *,
    system: str,
    tools: list[dict],
    messages: list[dict],
    client=None,
    model: str | None = None,
    max_tokens: int | None = None,
) -> ModelResponse:
    """Make one request. Reports failure; never raises."""
    started = time.perf_counter()
    model = model or config.AGENT_MODEL

    def _elapsed_ms() -> int:
        return int((time.perf_counter() - started) * 1000)

    try:
        response = await (client or get_client()).messages.create(
            model=model,
            max_tokens=max_tokens or config.AGENT_MAX_TOKENS,
            system=system,
            tools=tools,
            messages=messages,
            # No output_config: "effort" is a 400 on Sonnet 4.5.
            thinking={
                "type": "enabled",
                "budget_tokens": config.AGENT_THINKING_BUDGET,
            },
        )
    except Exception as exc:  # noqa: BLE001 - reports, does not raise
        return ModelResponse(
            ok=False,
            error=f"{type(exc).__name__}: {exc}",
            latency_ms=_elapsed_ms(),
        )

    usage = getattr(response, "usage", None)
    input_tokens = getattr(usage, "input_tokens", 0) or 0
    output_tokens = getattr(usage, "output_tokens", 0) or 0
    cache_read = getattr(usage, "cache_read_input_tokens", 0) or 0
    cache_creation = getattr(usage, "cache_creation_input_tokens", 0) or 0

    stop_reason = getattr(response, "stop_reason", None)
    details = getattr(response, "stop_details", None)
    refusal = None
    if stop_reason == "refusal" and details is not None:
        refusal = (
            f"{getattr(details, 'category', 'unknown')}: "
            f"{getattr(details, 'explanation', '')}"
        ).strip()

    return ModelResponse(
        content=[_as_block(block) for block in getattr(response, "content", [])],
        stop_reason=stop_reason,
        refusal=refusal,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_read_tokens=cache_read,
        cache_creation_tokens=cache_creation,
        cost_usd=estimate_cost(
            model, input_tokens, output_tokens, cache_read, cache_creation
        ),
        latency_ms=_elapsed_ms(),
    )

"""Shared primitives every agent tool returns.

Two rules govern the tool layer, and both are deliberate divergences from the
``ValueError``/``RuntimeError`` convention used elsewhere in the repo:

1. **A tool never raises for an expected failure.** Prometheus down, the Docker
   socket missing, an unknown metric name — all come back as ``ok=False`` with a
   readable ``error``. The caller is an LLM, not a programmer: a raised
   exception kills the graph, whereas an error observation lets the agent adapt
   and try a different tool.
2. **Empty is not failure.** ``ok=True`` with zero rows is a first-class answer.
   "No deploys in the window" rules out a code change; traffic going flat *is*
   the evidence under a timeout fault. Conflating the two would blind the agent
   to its most useful negative signals.

Every result also carries ``source``, ``query`` and ``window`` — the literal
query issued and the range it covered — because that triple is what makes a
cited diagnosis mechanically possible.
"""

from datetime import datetime, timedelta, timezone
from typing import Callable, Type, TypeVar

from pydantic import BaseModel, Field, field_validator, model_validator


def utc_now() -> datetime:
    """Return the current time as a timezone-aware UTC datetime.

    Injected as ``now=`` throughout the tool layer so tests get a deterministic
    clock without a mocking library.
    """
    return datetime.now(timezone.utc)


def _as_utc(value: datetime) -> datetime:
    """Coerce a datetime to UTC, assuming UTC for a naive one.

    Assuming UTC (rather than local time) is the important half: ``docker-py``
    interprets a naive datetime as *local* time, which silently shifts every log
    window by the host's offset.
    """
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


class TimeWindow(BaseModel):
    """A closed [start, end] range, always stored in UTC."""

    start: datetime
    end: datetime

    @field_validator("start", "end")
    @classmethod
    def _normalise_to_utc(cls, value: datetime) -> datetime:
        return _as_utc(value)

    @model_validator(mode="after")
    def _check_ordering(self) -> "TimeWindow":
        if self.end < self.start:
            raise ValueError(
                f"window end {self.end.isoformat()} precedes start "
                f"{self.start.isoformat()}"
            )
        return self

    @classmethod
    def lookback(
        cls, minutes: int, *, now: Callable[[], datetime] = utc_now
    ) -> "TimeWindow":
        """Return the window covering the last ``minutes`` minutes."""
        end = _as_utc(now())
        return cls(start=end - timedelta(minutes=minutes), end=end)

    @property
    def unix_start(self) -> int:
        """Start as an integer unix timestamp (what Docker and Prometheus want)."""
        return int(self.start.timestamp())

    @property
    def unix_end(self) -> int:
        """End as an integer unix timestamp."""
        return int(self.end.timestamp())

    @property
    def duration_minutes(self) -> float:
        return (self.end - self.start).total_seconds() / 60.0


class ToolResult(BaseModel):
    """The envelope shared by every tool's result.

    Each tool subclasses this and adds its own typed rows. Dumped with
    ``model_dump(mode="json")`` it drops straight into the ``evidence`` jsonb
    column on ``investigations``.
    """

    tool: str
    ok: bool = True
    summary: str  # one line, written for the LLM to reason on
    source: str  # e.g. "prometheus @ http://prometheus:9090/api/v1/query_range"
    query: str  # the literal query issued — this is the citation
    window: TimeWindow | None = None
    truncated: bool = False
    notes: list[str] = Field(default_factory=list)
    error: str | None = None
    latency_ms: int = 0


class ActionInput(BaseModel):
    """Every action names the service it acts on.

    Required, and on the base class, because the policy gate compares that
    target against the service the hypothesis blamed — an action with no target
    could not be checked.
    """

    service: str = Field(
        description="The service to act on: api-gateway, data-service, or downstream-dep."
    )


class ActionResult(ToolResult):
    """What an action did, or would have done.

    Lives beside ``ToolResult`` rather than in ``actions.py`` so that each write
    tool can import its envelope without importing the registry that imports
    *it*. Everything ToolResult promises holds here too, the never-raises rule
    included.

    ``executed`` is the honest record of whether the world changed;
    ``verification`` is the independent check afterwards, in the action's own
    words ("health returned 200 after 1.2s"), so a report never rests on the
    action merely claiming success.
    """

    executed: bool = False
    dry_run: bool = False
    target: str = ""
    verification: str | None = None


def action_query(name: str, arguments: ActionInput) -> str:
    """The literal call issued, as the citation for this action."""
    fields = ", ".join(
        f"{k}={v}" for k, v in arguments.model_dump().items() if v is not None
    )
    return f"{name}({fields})"


ResultT = TypeVar("ResultT", bound=ToolResult)


def failure(
    *,
    tool: str,
    source: str,
    query: str,
    error: str,
    summary: str,
    model: Type[ResultT] = ToolResult,
    window: TimeWindow | None = None,
    latency_ms: int = 0,
) -> ResultT:
    """Build the ``ok=False`` envelope for an expected failure.

    ``model`` lets each tool return *its own* Result subclass, so a tool's
    declared return type holds on the error path too.
    """
    return model(
        tool=tool,
        ok=False,
        summary=summary,
        source=source,
        query=query,
        window=window,
        error=error,
        latency_ms=latency_ms,
    )

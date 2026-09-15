"""toggle_config - return a service's runtime configuration to its default.

**What this actually does, stated plainly.** It sends ``DELETE /admin/fault`` to
the target service. In this synthetic stack the admin fault surface *is* the
runtime config surface: ``bad_config`` is a flag in process memory
(services/data-service/app/faults.py) and clearing it is exactly what "revert the
bad config" means here. A dedicated ``/admin/config`` endpoint holding a real
config document would be the cleaner design and would make this tool narrower
than "clear whatever fault is set" - it is deliberately deferred, because adding
it reopens Stage 1 (the victim system) to serve Stage 3.

The consequence worth knowing: this endpoint clears *any* active fault on the
service, not only a config one. That breadth is precisely why the policy gate,
not this tool, decides when it may run - ACTION_ADDRESSES restricts it to the
mechanism it is meant for.

**Verification here is self-reported.** There is no ``GET /admin/fault`` to read
state back, so the tool can only report what the service said. The independent
check is query_metrics: config_errors_total stops climbing.
"""

import time
from typing import Awaitable, Callable

import httpx

from agent import config
from agent.tools.base import ActionInput, ActionResult, action_query, failure

TOOL_NAME = "toggle_config"

ADMIN_PATH = "/admin/fault"
HTTP_TIMEOUT_SECONDS = 5.0

# How much of an error body is worth carrying. The summary is re-read by the
# model on every turn, so it is not a dumping ground for a stack trace.
MAX_BODY_CHARS = 200


class ToggleInput(ActionInput):
    """Which service's runtime configuration to reset."""


class ToggleResult(ActionResult):
    """No extra fields: the envelope says everything a reset did."""


async def _http_delete(url: str) -> tuple[int, str]:
    """DELETE ``url`` and return (status, body). Raises for a transport failure."""
    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT_SECONDS) as client:
        response = await client.delete(url)
        return response.status_code, response.text


def _clip(body: str) -> str:
    body = " ".join(body.split())
    return body if len(body) <= MAX_BODY_CHARS else body[:MAX_BODY_CHARS] + "..."


async def toggle_config(
    q: ToggleInput,
    *,
    delete: Callable[[str], Awaitable[tuple[int, str]]] = _http_delete,
) -> ToggleResult:
    """Reset ``service`` to its default runtime configuration."""
    started = time.perf_counter()
    query = action_query(TOOL_NAME, q)

    def _elapsed_ms() -> int:
        return int((time.perf_counter() - started) * 1000)

    def _failed(*, source: str, error: str, summary: str) -> ToggleResult:
        return failure(
            tool=TOOL_NAME, source=source, query=query, error=error, summary=summary,
            model=ToggleResult, latency_ms=_elapsed_ms(),
        )

    try:
        url = config.service_url(q.service) + ADMIN_PATH
    except ValueError as exc:
        # Strict core, forgiving boundary: service_url raises, the tool reports.
        return _failed(
            source=f"DELETE {ADMIN_PATH}",
            error=str(exc),
            summary=(
                f"There is no service called '{q.service}' to reconfigure. Known: "
                f"{', '.join(sorted(config.SERVICE_PORTS))}."
            ),
        )

    source = f"DELETE {url}"

    try:
        status, body = await delete(url)
    except httpx.TimeoutException:
        return _failed(
            source=source,
            error=f"'{q.service}' timed out after {HTTP_TIMEOUT_SECONDS:.0f}s at {url}",
            summary=(
                f"'{q.service}' did not answer in time; its configuration was not "
                f"changed."
            ),
        )
    except Exception as exc:  # noqa: BLE001 - any transport failure reads the same
        return _failed(
            source=source,
            error=f"'{q.service}' unreachable at {url}: {exc}",
            summary=(
                f"Could not reach '{q.service}'; its configuration was not changed."
            ),
        )

    if status != 200:
        return _failed(
            source=source,
            error=f"'{q.service}' answered HTTP {status}: {_clip(body)}",
            summary=(
                f"'{q.service}' refused the reset with HTTP {status}; nothing was "
                f"changed."
            ),
        )

    verification = f"{ADMIN_PATH} returned {status} ({_clip(body)})"

    return ToggleResult(
        tool=TOOL_NAME,
        ok=True,
        summary=(
            f"Reset '{q.service}' to its default runtime configuration; the service "
            f"confirmed ({verification})."
        ),
        source=source,
        query=query,
        target=q.service,
        executed=True,
        verification=verification,
        notes=[
            "This confirmation is the service's own; confirm independently with "
            "query_metrics that config_errors_total has stopped climbing."
        ],
        latency_ms=_elapsed_ms(),
    )

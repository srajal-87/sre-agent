"""restart_service - restart one victim container and check it came back.

Every fault in this stack lives in process memory (services/*/app/faults.py), so
a restart genuinely clears one. That is honest here and would not be in a real
system, where a restart is a blunt instrument that loses in-flight work - which
is why the policy gate, not this tool, decides when it is allowed to run.

Two conventions this module is careful about:

* ``ok`` says whether the action did its job; ``verification`` says whether the
  world got better. A restart that ran but left the service unhealthy is
  ``ok=True, executed=True`` with a verification that says so. Collapsing the
  two would make "restarted, still sick" indistinguishable from "could not
  restart", and those call for opposite next moves.
* The health poll retries. A container that has just restarted refuses
  connections for a moment, so treating the first ConnectionError as the verdict
  would fail every real restart.
"""

import asyncio
import time
from typing import Awaitable, Callable

import httpx

from agent import config
from agent.tools.base import ActionInput, ActionResult, action_query, failure
from agent.tools.containers import connect, find_container

TOOL_NAME = "restart_service"

HEALTH_PATH = "/health"
HEALTH_TIMEOUT_SECONDS = 3.0

# How long docker waits after SIGTERM before SIGKILL. Stated rather than
# inherited from docker-py's default, and separate from ACTION_TIMEOUT_SECONDS:
# this one is the container's grace period, not the action's overall bound.
STOP_GRACE_SECONDS = 10

# The poll after the restart. Ten attempts a second apart comfortably covers a
# FastAPI container's cold start without approaching ACTION_TIMEOUT_SECONDS.
HEALTH_ATTEMPTS = 10
HEALTH_INTERVAL_SECONDS = 1.0


class RestartInput(ActionInput):
    """Which container to restart."""


class RestartResult(ActionResult):
    """No extra fields: what a restart did is fully said by the envelope."""


async def _http_status(url: str) -> int:
    """GET ``url`` and return the status code. Raises for a connection failure."""
    async with httpx.AsyncClient(timeout=HEALTH_TIMEOUT_SECONDS) as client:
        response = await client.get(url)
        return response.status_code


async def _poll_health(
    service: str,
    *,
    fetch: Callable[[str], Awaitable[int]],
    sleep: Callable[[float], Awaitable[None]],
) -> tuple[bool, str]:
    """Poll the service's /health until it answers 200. Returns (healthy, note)."""
    try:
        url = config.service_url(service) + HEALTH_PATH
    except ValueError as exc:  # unreachable via the guard above, cheap to keep
        return False, f"could not verify: {exc}"

    last = "no response"
    for attempt in range(1, HEALTH_ATTEMPTS + 1):
        try:
            status = await fetch(url)
        except Exception as exc:  # noqa: BLE001 - a restarting container refuses connections
            last = f"{type(exc).__name__}: {exc}"
        else:
            if status == 200:
                return True, f"health returned 200 after {attempt} attempt(s) at {url}"
            last = f"HTTP {status}"
        if attempt < HEALTH_ATTEMPTS:
            await sleep(HEALTH_INTERVAL_SECONDS)

    return False, (
        f"health did not return 200 within {HEALTH_ATTEMPTS} attempt(s) at {url} "
        f"(last: {last})"
    )


async def restart_service(
    q: RestartInput,
    *,
    docker_client=None,
    project: str | None = None,
    fetch: Callable[[str], Awaitable[int]] = _http_status,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> RestartResult:
    """Restart the container running ``service``, then verify its health.

    docker-py is synchronous, so both the lookup and the restart happen in a
    worker thread, matching query_logs.
    """
    started = time.perf_counter()
    project = project or config.COMPOSE_PROJECT
    source = f"docker restart (compose project '{project}')"
    query = action_query(TOOL_NAME, q)

    def _elapsed_ms() -> int:
        return int((time.perf_counter() - started) * 1000)

    def _failed(error: str, summary: str) -> RestartResult:
        return failure(
            tool=TOOL_NAME, source=source, query=query, error=error, summary=summary,
            model=RestartResult, latency_ms=_elapsed_ms(),
        )

    if q.service not in config.LOG_SERVICES:
        return _failed(
            error=(
                f"unknown service '{q.service}'; known: {sorted(config.LOG_SERVICES)}"
            ),
            summary=(
                f"There is no service called '{q.service}' to restart. Known: "
                f"{', '.join(config.LOG_SERVICES)}."
            ),
        )

    try:
        client = docker_client
        if client is None:
            client = await asyncio.to_thread(connect)
        container = await asyncio.to_thread(find_container, client, project, q.service)
    except Exception as exc:  # noqa: BLE001 - DockerException needs the lazy import
        return _failed(
            error=(
                f"docker api unavailable ({exc}); restarting a container requires "
                f"/var/run/docker.sock to be mounted into this container"
            ),
            summary="Could not reach the Docker API, so nothing was restarted.",
        )

    if container is None:
        return _failed(
            error=(
                f"no container for '{q.service}' in compose project '{project}'"
            ),
            summary=(
                f"There is no '{q.service}' container to restart in compose project "
                f"'{project}'; nothing was changed."
            ),
        )

    try:
        await asyncio.to_thread(container.restart, timeout=STOP_GRACE_SECONDS)
    except Exception as exc:  # noqa: BLE001 - same lazy-import reason
        return _failed(
            error=f"restart of '{q.service}' failed: {exc}",
            summary=f"The restart of '{q.service}' did not complete: {exc}",
        )

    healthy, verification = await _poll_health(q.service, fetch=fetch, sleep=sleep)

    if healthy:
        summary = f"Restarted '{q.service}'; it is serving again ({verification})."
        notes: list[str] = []
    else:
        summary = (
            f"Restarted '{q.service}', but it did not come back healthy "
            f"({verification})."
        )
        notes = [
            f"'{q.service}' was restarted and is still not healthy; the fault may "
            f"not be in that service, or it may not be restart-clearable."
        ]

    return RestartResult(
        tool=TOOL_NAME,
        ok=True,
        summary=summary,
        source=source,
        query=query,
        target=q.service,
        executed=True,
        verification=verification,
        notes=notes,
        latency_ms=_elapsed_ms(),
    )

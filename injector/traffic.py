"""Traffic generator.

Prometheus counters and histograms only move when requests flow, so a fault
injected into a silent stack produces no metrics at all. This drives a steady
request rate at a victim service for the duration of a fault window.

Requests are issued **sequentially**, not concurrently. At 5 rps against a
3-second latency fault a concurrent generator would pile up hundreds of
in-flight requests and turn a latency fault into an accidental load test — the
metrics would then show queue saturation rather than the injected delay.
Sequential pacing means the generator itself slows down under a latency fault,
exactly as a real client would, and the request rate visibly dropping is
legitimate evidence. Achieved rate therefore falls below --rps during a fault;
the report prints both so that is never mistaken for a bug.

Usage:
    python injector/traffic.py --target api-gateway --rps 5 --duration 300
"""

import argparse
import asyncio
import time
from dataclasses import dataclass, field
from typing import Awaitable, Callable

import httpx

try:  # package import: the tests, and `python -m injector.traffic`
    from injector.inject import resolve_target
except ModuleNotFoundError:  # direct script run: `python injector/traffic.py`
    from inject import resolve_target

# The request-serving endpoint on each victim service. /request fans out through
# all three hops, which is what makes it the useful default.
TARGET_PATHS = {
    "api-gateway": "/request",
    "data-service": "/process",
    "downstream-dep": "/data",
}

REQUEST_TIMEOUT_SECONDS = 10.0


@dataclass
class TrafficReport:
    """What the run actually achieved."""

    sent: int = 0
    errors: int = 0
    status_counts: dict[int, int] = field(default_factory=dict)
    elapsed_seconds: float = 0.0
    requested_rps: float = 0.0

    @property
    def achieved_rps(self) -> float:
        if self.elapsed_seconds <= 0:
            return 0.0
        return self.sent / self.elapsed_seconds


def _default_requester(url: str) -> int:
    """Issue one request, returning its status code.

    Uses one keep-alive client for the whole run: a fresh connection per request
    costs more than the three-hop call itself and would understate the rate the
    stack can actually serve.
    """
    global _client
    if _client is None:
        _client = httpx.Client(timeout=REQUEST_TIMEOUT_SECONDS)
    return _client.get(url).status_code


_client: httpx.Client | None = None


async def run(
    *,
    target: str,
    rps: int,
    duration: int,
    requester: Callable[[str], int] = _default_requester,
    clock: Callable[[], float] = time.monotonic,
    sleeper: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> TrafficReport:
    """Send ``rps * duration`` requests to ``target``, paced at ``rps``.

    A request that raises is counted in ``errors`` and never propagates —
    traffic must survive the fault it exists to expose.
    """
    if rps <= 0:
        raise ValueError(f"rps must be positive, got {rps}")
    if duration <= 0:
        raise ValueError(f"duration must be positive, got {duration}")

    url = resolve_target(target) + TARGET_PATHS[target]
    interval = 1.0 / rps
    total = rps * duration

    report = TrafficReport(requested_rps=float(rps))
    start = clock()

    for index in range(total):
        try:
            status = requester(url)
        except Exception:  # noqa: BLE001 — any transport error is just a data point
            report.errors += 1
        else:
            report.status_counts[status] = report.status_counts.get(status, 0) + 1
        report.sent += 1

        # Pace against the schedule, not against the previous request, so a slow
        # request doesn't permanently shift every one after it.
        next_due = start + (index + 1) * interval
        await sleeper(max(0.0, next_due - clock()))

    report.elapsed_seconds = clock() - start
    return report


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Generate traffic against a victim service.")
    parser.add_argument("--target", default="api-gateway", help="target service name")
    parser.add_argument("--rps", type=int, default=5, help="requests per second")
    parser.add_argument("--duration", type=int, default=60, help="seconds to run")
    args = parser.parse_args(argv)

    if args.target not in TARGET_PATHS:
        raise ValueError(
            f"unknown target '{args.target}'; known: {sorted(TARGET_PATHS)}"
        )

    report = asyncio.run(
        run(target=args.target, rps=args.rps, duration=args.duration)
    )

    statuses = ", ".join(f"{code}: {n}" for code, n in sorted(report.status_counts.items()))
    print(
        f"sent {report.sent} requests to {args.target} in "
        f"{report.elapsed_seconds:.1f}s "
        f"({report.achieved_rps:.1f} rps achieved of {report.requested_rps:.0f} requested)"
    )
    print(f"statuses: {statuses or 'none'}; errors: {report.errors}")


if __name__ == "__main__":
    main()

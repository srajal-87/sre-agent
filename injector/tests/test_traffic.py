"""The traffic generator: rate pacing, outcome tallying, error tolerance."""

import asyncio

import pytest

from injector import traffic


def _run(coro):
    """Drive a coroutine, matching the repo's no-pytest-asyncio convention."""
    return asyncio.run(coro)


class _Clock:
    """A fake monotonic clock advanced only by the sleeps the generator issues."""

    def __init__(self):
        self.t = 0.0
        self.sleeps = []

    def now(self) -> float:
        return self.t

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.t += max(seconds, 0.0)


def test_resolve_target_reuses_the_injector_port_map():
    assert traffic.resolve_target("api-gateway").endswith(":8001")


def test_unknown_target_raises():
    with pytest.raises(ValueError):
        traffic.resolve_target("nope")


def test_sends_rps_times_duration_requests():
    clock = _Clock()
    sent = []

    report = _run(
        traffic.run(
            target="api-gateway",
            rps=5,
            duration=4,
            requester=lambda url: sent.append(url) or 200,
            clock=clock.now,
            sleeper=clock.sleep,
        )
    )

    assert len(sent) == 20
    assert report.sent == 20
    assert sent[0].endswith("/request")


def test_paces_requests_at_the_requested_interval():
    clock = _Clock()

    _run(
        traffic.run(
            target="api-gateway",
            rps=4,
            duration=1,
            requester=lambda url: 200,
            clock=clock.now,
            sleeper=clock.sleep,
        )
    )

    # 4 rps -> one request every 0.25s.
    assert all(abs(s - 0.25) < 1e-9 for s in clock.sleeps if s > 0)


def test_never_sleeps_a_negative_interval_when_requests_run_slow():
    """A request slower than the interval must not push the sleep below zero."""
    clock = _Clock()

    def slow(url):
        clock.t += 1.0  # each request takes longer than the 0.25s budget
        return 200

    _run(
        traffic.run(
            target="api-gateway",
            rps=4,
            duration=1,
            requester=slow,
            clock=clock.now,
            sleeper=clock.sleep,
        )
    )

    assert all(s >= 0 for s in clock.sleeps)


def test_tallies_status_codes():
    clock = _Clock()
    codes = iter([200, 200, 504, 502])

    report = _run(
        traffic.run(
            target="api-gateway",
            rps=4,
            duration=1,
            requester=lambda url: next(codes),
            clock=clock.now,
            sleeper=clock.sleep,
        )
    )

    assert report.status_counts == {200: 2, 504: 1, 502: 1}


def test_a_failed_request_is_counted_not_raised():
    """Traffic must survive the fault it exists to expose."""
    clock = _Clock()

    def boom(url):
        raise RuntimeError("connection refused")

    report = _run(
        traffic.run(
            target="api-gateway",
            rps=2,
            duration=1,
            requester=boom,
            clock=clock.now,
            sleeper=clock.sleep,
        )
    )

    assert report.sent == 2
    assert report.errors == 2
    assert report.status_counts == {}


def test_rps_must_be_positive():
    with pytest.raises(ValueError):
        _run(traffic.run(target="api-gateway", rps=0, duration=1))

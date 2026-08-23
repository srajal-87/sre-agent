"""Seed the synthetic deploy ledger.

There is no real deploy history in this project, so this generates a plausible
one for the agent's query_deploy_history tool to read.

**The noise is the point.** If every deploy preceded a fault, "a deploy exists"
would trivially imply causation and the Phase 5 evaluation would be meaningless.
These deploys are correlated with nothing: they are spread across all three
services at arbitrary times, so the agent has to reason from *timing and service
match* rather than from the mere existence of a deploy. The deploy correlated
with an actual fault is written separately, by inject.py.

For the same reason, messages hint at a plausible cause without naming the fault
("tune connection pool timeouts", never "add 3s sleep") — otherwise the agent
reads the answer straight off the commit subject.

Usage:
    python injector/seed_deploys.py --noise 5
    python injector/seed_deploys.py --noise 10 --hours 12 --seed 42
    python injector/seed_deploys.py --clear --noise 0
"""

import argparse
import asyncio
import json
import os
import random
from datetime import datetime, timedelta, timezone
from typing import Callable

SERVICES = ["api-gateway", "data-service", "downstream-dep"]

AUTHORS = ["alex.chen", "priya.n", "sam.okafor", "mira.dvorak", "j.whitfield"]

# Neutral commit subjects: each suggests a plausible cause, none names a fault.
MESSAGES = [
    # Not "tune connection pool timeouts": 'timeout' is a fault name, and an
    # agent that lexically matches it against the fault has not reasoned at all.
    ("tune connection pool settings", ["app/config.py", "app/client.py"]),
    ("add request id to access log", ["app/main.py"]),
    ("bump base image to python 3.12.4", ["Dockerfile"]),
    ("refactor configuration loading", ["app/config.py"]),
    ("cache upstream responses for repeat keys", ["app/main.py", "app/cache.py"]),
    ("raise worker concurrency", ["Dockerfile", "app/main.py"]),
    ("reduce serialization overhead on the hot path", ["app/serializers.py"]),
    ("add retry to the downstream client", ["app/client.py"]),
    ("update dependency pins", ["requirements.txt"]),
    ("adjust health check interval", ["app/main.py", "docker-compose.yml"]),
    ("split request handler into smaller units", ["app/main.py", "app/handlers.py"]),
    ("emit structured logs for admin routes", ["app/faults.py", "app/logging.py"]),
]

# Occasional non-success rows keep the ledger honest; the agent should not
# assume every deploy landed cleanly.
STATUS_WEIGHTS = [("succeeded", 90), ("failed", 6), ("rolled_back", 4)]

DEFAULT_HOURS = 6

DEPLOYS_TABLE = "deploys"


def _weighted_status(rng: random.Random) -> str:
    population = [status for status, _ in STATUS_WEIGHTS]
    weights = [weight for _, weight in STATUS_WEIGHTS]
    return rng.choices(population, weights=weights, k=1)[0]


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def generate_deploys(
    count: int,
    *,
    hours: int = DEFAULT_HOURS,
    seed: int | None = None,
    now: Callable[[], datetime] = _utc_now,
) -> list[dict]:
    """Build ``count`` uncorrelated deploy records, most recent first.

    Versions increase with time within each service: a ledger where v1.0.0 ships
    after v2.0.0 reads as corrupt.
    """
    rng = random.Random(seed)
    end = now()

    # Sampled without replacement so no two deploys share a second: colliding
    # timestamps make "which shipped last" ambiguous, which is the one question
    # this ledger exists to answer.
    span_seconds = hours * 3600
    offsets = (
        rng.sample(range(span_seconds + 1), count)
        if count <= span_seconds
        else [rng.randint(0, span_seconds) for _ in range(count)]
    )
    stamps = sorted(end - timedelta(seconds=offset) for offset in offsets)

    # One version counter per service, bumped in chronological order.
    next_version = {service: [1, rng.randint(0, 9), 0] for service in SERVICES}

    records = []
    for deployed_at in stamps:
        service = rng.choice(SERVICES)
        major, minor, patch = next_version[service]
        if rng.random() < 0.25:
            minor, patch = minor + 1, 0
        else:
            patch += 1
        next_version[service] = [major, minor, patch]

        message, changed_files = rng.choice(MESSAGES)
        records.append(
            {
                "service": service,
                "version": f"v{major}.{minor}.{patch}",
                "commit_sha": f"{rng.getrandbits(28):07x}",
                "author": rng.choice(AUTHORS),
                "message": message,
                "changed_files": list(changed_files),
                "deployed_at": deployed_at,
                "status": _weighted_status(rng),
            }
        )

    records.reverse()  # most recent first
    return records


def seed(
    *,
    noise: int,
    writer: Callable[[list[dict]], None],
    clearer: Callable[[], None] | None = None,
    clear: bool = False,
    hours: int = DEFAULT_HOURS,
    seed: int | None = None,
    now: Callable[[], datetime] = _utc_now,
) -> list[dict]:
    """Generate and persist ``noise`` deploys. Returns what was generated."""
    if clear:
        if clearer is None:
            raise ValueError("clear=True requires a clearer")
        clearer()

    records = generate_deploys(noise, hours=hours, seed=seed, now=now)
    if records:
        writer(records)
    return records


# ── persistence ──────────────────────────────────────────────────────


INSERT_SQL = (
    f"insert into {DEPLOYS_TABLE} "
    "(service, version, commit_sha, author, message, changed_files, "
    " deployed_at, status) "
    "values (:service, :version, :commit_sha, :author, :message, "
    "        cast(:changed_files as jsonb), :deployed_at, :status)"
)


async def _execute(statement: str, rows: list[dict] | None = None) -> None:
    """Run one statement against DATABASE_URL.

    Imports are lazy and the engine is per-call: this is a one-shot CLI, and
    keeping the driver out of import time is what lets the generator above be
    tested with no database configured. Uses the same asyncpg driver as the app
    rather than adding a second one for a synchronous path.
    """
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine

    dsn = os.getenv("DATABASE_URL", "")
    if not dsn:
        raise RuntimeError(
            "DATABASE_URL is not set. Copy .env.example to .env and set the "
            "Supabase session-pooler DSN."
        )

    engine = create_async_engine(dsn)
    try:
        async with engine.begin() as connection:
            await connection.execute(text(statement), rows)
    finally:
        await engine.dispose()


def write_deploys(records: list[dict]) -> None:
    rows = [{**r, "changed_files": json.dumps(r["changed_files"])} for r in records]
    asyncio.run(_execute(INSERT_SQL, rows))


def clear_deploys() -> None:
    asyncio.run(_execute(f"delete from {DEPLOYS_TABLE}"))


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Seed the synthetic deploy ledger.")
    parser.add_argument("--noise", type=int, default=5, help="uncorrelated deploys to write")
    parser.add_argument("--hours", type=int, default=DEFAULT_HOURS, help="window to spread them over")
    parser.add_argument("--seed", type=int, default=None, help="RNG seed, for a reproducible ledger")
    parser.add_argument("--clear", action="store_true", help="delete every existing deploy first")
    args = parser.parse_args(argv)

    records = seed(
        noise=args.noise,
        writer=write_deploys,
        clearer=clear_deploys,
        clear=args.clear,
        hours=args.hours,
        seed=args.seed,
    )

    if args.clear:
        print("cleared the deploy ledger")
    print(f"wrote {len(records)} noise deploy(s) over the last {args.hours}h")
    for record in records:
        print(
            f"  {record['deployed_at']:%Y-%m-%dT%H:%M:%SZ}  {record['service']:15}"
            f"  {record['version']:9}  {record['status']:11}  {record['message']}"
        )


if __name__ == "__main__":
    main()

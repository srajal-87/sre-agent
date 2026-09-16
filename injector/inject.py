"""Fault injector CLI.

Enables a fault on a victim service via its /admin/fault endpoint, optionally
waits a fixed window then auto-reverts, and records ground truth (which fault,
where, when) to a JSONL file for the future evaluation harness.

Usage:
    python injector/inject.py --fault timeout --target api-gateway --duration 60
    python injector/inject.py --fault latency --target downstream-dep \
        --params '{"delay_ms": 3000}' --duration 30
    python injector/inject.py --fault timeout --target api-gateway --clear
"""

import argparse
import asyncio
import json
import os
import random
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

import httpx

try:  # package import: the tests, and `python -m injector.inject`
    from injector.seed_deploys import AUTHORS, write_deploys
except ModuleNotFoundError:  # direct script run: `python injector/inject.py`
    from seed_deploys import AUTHORS, write_deploys

# Host ports as mapped in docker-compose.yml.
TARGET_PORTS = {
    "api-gateway": 8001,
    "data-service": 8002,
    "downstream-dep": 8003,
}

GROUND_TRUTH_PATH = Path(__file__).with_name("ground_truth.jsonl")

# How often a fault gets a preceding deploy. Deliberately not 1.0: if every
# fault had one, "a deploy exists" would trivially imply causation and the
# Phase 5 evaluation would measure nothing.
DEPLOY_PROBABILITY = 0.7

# How long before the fault the correlated deploy lands.
DEPLOY_LEAD_MINUTES_MIN = 1
DEPLOY_LEAD_MINUTES_MAX = 10

# Commit subjects plausible for each fault's *area*, none naming the fault
# itself: the agent must correlate on timing and service, not read the answer
# off the message. Kept in step with seed_deploys.MESSAGES.
FAULT_DEPLOY_MESSAGES = {
    "latency": [
        ("cache upstream responses for repeat keys", ["app/main.py", "app/cache.py"]),
        ("reduce serialization overhead on the hot path", ["app/serializers.py"]),
        ("add retry to the downstream client", ["app/client.py"]),
    ],
    "timeout": [
        ("tune connection pool settings", ["app/config.py", "app/client.py"]),
        ("raise worker concurrency", ["Dockerfile", "app/main.py"]),
    ],
    "bad_config": [
        ("refactor configuration loading", ["app/config.py"]),
        ("update dependency pins", ["requirements.txt"]),
    ],
    "memory": [
        ("cache upstream responses for repeat keys", ["app/main.py", "app/cache.py"]),
        ("raise worker concurrency", ["Dockerfile", "app/main.py"]),
    ],
}

# Used when the fault type has no dedicated pool, so a new fault type never
# crashes the injector.
NEUTRAL_DEPLOY_MESSAGES = [
    ("split request handler into smaller units", ["app/main.py", "app/handlers.py"]),
    ("bump base image to python 3.12.4", ["Dockerfile"]),
    ("adjust health check interval", ["app/main.py", "docker-compose.yml"]),
]


def resolve_target(target: str) -> str:
    """Return the base URL for a target, honouring an env override.

    Override with e.g. API_GATEWAY_URL=http://host:port for a target named
    "api-gateway".
    """
    if target not in TARGET_PORTS:
        raise ValueError(
            f"unknown target '{target}'; known: {sorted(TARGET_PORTS)}"
        )
    env_key = target.upper().replace("-", "_") + "_URL"
    override = os.getenv(env_key)
    if override:
        return override
    return f"http://localhost:{TARGET_PORTS[target]}"


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def write_ground_truth(record: dict, gt_path: Path) -> None:
    """Append one ground-truth record as a JSON line."""
    gt_path.parent.mkdir(parents=True, exist_ok=True)
    with gt_path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record) + "\n")


def _default_poster(url: str, payload: dict) -> None:
    resp = httpx.post(f"{url}/admin/fault", json=payload, timeout=5.0)
    resp.raise_for_status()


def _utc_now_dt() -> datetime:
    return datetime.now(timezone.utc)


def build_correlated_deploy(
    *, fault: str, target: str, fault_started_at: datetime, rng: random.Random
) -> dict:
    """Build the deploy row that will look like a plausible cause of ``fault``."""
    pool = FAULT_DEPLOY_MESSAGES.get(fault, NEUTRAL_DEPLOY_MESSAGES)
    message, changed_files = rng.choice(pool)
    lead = rng.uniform(DEPLOY_LEAD_MINUTES_MIN, DEPLOY_LEAD_MINUTES_MAX)
    return {
        "service": target,
        "version": f"v{rng.randint(1, 3)}.{rng.randint(0, 9)}.{rng.randint(0, 9)}",
        "commit_sha": f"{rng.getrandbits(28):07x}",
        "author": rng.choice(AUTHORS),
        "message": message,
        "changed_files": list(changed_files),
        "deployed_at": fault_started_at - timedelta(minutes=lead),
        "status": "succeeded",
    }


def _as_datetime(value: str | datetime | None) -> datetime | None:
    """A '...Z' ground-truth timestamp as a real datetime.

    timestamptz wants a datetime, not a string - the same way seed_deploys
    passes ``deployed_at``. ``fromisoformat`` only learned 'Z' in 3.11, so it is
    normalised first.
    """
    if value is None or isinstance(value, datetime):
        return value
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def incident_row(record: dict, payload: dict | None = None) -> dict:
    """The column values for one injected fault.

    A pure mapper, exactly like ``api/app/records.investigation_row``: it builds
    a dict and hands it back, while the session and the transaction stay in the
    writer below. That split is what makes the row's shape testable with nothing
    to connect to - and this row has several constraints that only fail live.

    ``payload`` is the scenario webhook the harness already loaded.
    ``raw_payload`` is NOT NULL and every other row in that column holds an
    Alertmanager webhook, so putting the injector's own record there would break
    any consumer that reads it. ``source='injector'`` is the discriminator.
    """
    payload = payload or {}
    return {
        # A UUID, not the str the record holds: asyncpg rejects a str into a
        # uuid column at execute time, i.e. in the middle of a paid run.
        "id": uuid.UUID(record["incident_id"]),
        "source": "injector",
        # check (status in ('firing', 'resolved')) - a reverted fault is
        # resolved, one still running is firing.
        "status": "resolved" if record.get("ended_at") else "firing",
        "receiver": payload.get("receiver"),
        "group_key": payload.get("groupKey"),
        "service": record["target"],
        "common_labels": payload.get("commonLabels") or {},
        "alert_count": len(payload.get("alerts") or []),
        "raw_payload": payload,
        "ground_truth_fault": record["fault"],
        "ground_truth_target": record["target"],
        "fault_started_at": _as_datetime(record.get("started_at")),
        "fault_ended_at": _as_datetime(record.get("ended_at")),
    }


INCIDENT_UPSERT_SQL = (
    "insert into incidents "
    "(id, source, status, receiver, group_key, service, common_labels, "
    " alert_count, raw_payload, ground_truth_fault, ground_truth_target, "
    " fault_started_at, fault_ended_at) "
    "values (:id, :source, :status, :receiver, :group_key, :service, "
    "        cast(:common_labels as jsonb), :alert_count, "
    "        cast(:raw_payload as jsonb), :ground_truth_fault, "
    "        :ground_truth_target, :fault_started_at, :fault_ended_at) "
    # Removes a whole class of "re-run it and it explodes", and leaves the door
    # open to writing the row at enable time and completing it at revert.
    "on conflict (id) do update set "
    "  fault_ended_at = excluded.fault_ended_at, "
    "  status = excluded.status"
)


def write_incident(row: dict) -> None:
    """Upsert one ground-truth incident. The transaction lives here, not in the
    mapper - same split as ``api/app/repository.py``."""
    from injector.seed_deploys import _execute

    encoded = {
        **row,
        "common_labels": json.dumps(row["common_labels"]),
        "raw_payload": json.dumps(row["raw_payload"]),
    }
    asyncio.run(_execute(INCIDENT_UPSERT_SQL, [encoded]))


def run(
    *,
    fault: str,
    target: str,
    params: dict,
    duration: int | None,
    clear: bool,
    poster: Callable[[str, dict], None],
    sleeper: Callable[[float], None],
    gt_path: Path,
    now: Callable[[], str],
    deploy: bool | None = None,
    deploy_writer: Callable[[list[dict]], None] | None = None,
    deploy_clock: Callable[[], datetime] = _utc_now_dt,
    rng: random.Random | None = None,
    incident_id: str | None = None,
    truth_writer: Callable[[dict], None] | None = None,
    truth_payload: dict | None = None,
) -> dict | None:
    """Orchestrate an injection. Returns the ground-truth record, or None.

    - clear (or duration == 0): disable the fault only; no ground truth.
    - duration > 0: enable, wait, disable; full ground-truth window.
    - duration is None: enable and leave active; ground truth with ended_at=None.

    Before enabling the fault, a deploy for the target service is *sometimes*
    written to the ledger a few minutes earlier, so the agent has a real causal
    deploy to find. ``deploy=None`` decides at random (see DEPLOY_PROBABILITY);
    pass True/False to force it. Either way the outcome is recorded in ground
    truth, because Phase 5 has to score "no deploy caused this" as well.
    """
    url = resolve_target(target)

    if clear or duration == 0:
        poster(url, {"fault": fault, "enabled": False, "params": params})
        return None

    rng = rng or random.Random()
    should_deploy = rng.random() < DEPLOY_PROBABILITY if deploy is None else deploy

    correlated_deploy: dict | None = None
    deploy_error: str | None = None
    if should_deploy:
        row = build_correlated_deploy(
            fault=fault, target=target, fault_started_at=deploy_clock(), rng=rng
        )
        writer = deploy_writer or write_deploys
        try:
            writer([row])
        except Exception as exc:  # noqa: BLE001
            # Injecting the fault is the point; the deploy row is auxiliary. A
            # ledger outage must not stop an experiment, and ground truth stays
            # accurate: no deploy was written, and this says why.
            deploy_error = str(exc)
        else:
            correlated_deploy = {
                **{k: row[k] for k in
                   ("service", "version", "commit_sha", "author", "message")},
                "deployed_at": row["deployed_at"].isoformat(),
            }

    poster(url, {"fault": fault, "enabled": True, "params": params})
    started_at = now()
    ended_at: str | None = None

    if duration is not None:
        sleeper(duration)
        poster(url, {"fault": fault, "enabled": False, "params": params})
        ended_at = now()

    record = {
        # Minted here by default, but a caller can supply it: an investigation
        # of this fault runs *during* the sleep above, and the investigations
        # foreign key needs the id before this function has returned.
        "incident_id": incident_id or str(uuid.uuid4()),
        "fault": fault,
        "target": target,
        "params": params,
        "started_at": started_at,
        "ended_at": ended_at,
        # None means no deploy could have caused this fault - which Phase 5
        # must be able to score just as much as the positive case.
        "correlated_deploy": correlated_deploy,
        "deploy_error": deploy_error,
        "truth_error": None,
    }

    # Attempted BEFORE the JSONL is written, and swallowed the same way the
    # deploy write is: injecting the fault is the point. The order matters -
    # write the line first and this error would never be recorded anywhere.
    if truth_writer is not None:
        try:
            truth_writer(incident_row(record, truth_payload))
        except Exception as exc:  # noqa: BLE001
            record["truth_error"] = str(exc)

    write_ground_truth(record, gt_path)
    return record


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Inject a fault into a victim service.")
    parser.add_argument("--fault", required=True, help="fault type, e.g. timeout")
    parser.add_argument("--target", required=True, help="target service name")
    parser.add_argument(
        "--params", default="{}", help='JSON params, e.g. \'{"delay_ms": 3000}\''
    )
    parser.add_argument(
        "--duration",
        type=int,
        default=None,
        help="seconds to hold the fault before auto-revert; 0 or --clear reverts now",
    )
    parser.add_argument(
        "--clear", action="store_true", help="revert the fault immediately"
    )
    parser.add_argument(
        "--deploy",
        action=argparse.BooleanOptionalAction,
        default=None,
        help=(
            "write a correlated deploy before the fault (--no-deploy to skip); "
            f"omit to decide at random with p={DEPLOY_PROBABILITY}"
        ),
    )
    parser.add_argument(
        "--seed", type=int, default=None, help="RNG seed, for a reproducible run"
    )
    args = parser.parse_args(argv)

    params = json.loads(args.params)
    record = run(
        fault=args.fault,
        target=args.target,
        params=params,
        duration=args.duration,
        clear=args.clear,
        poster=_default_poster,
        sleeper=time.sleep,
        gt_path=GROUND_TRUTH_PATH,
        now=_utc_now_iso,
        deploy=args.deploy,
        rng=random.Random(args.seed),
        # Dual-written by default. A missing DATABASE_URL is not fatal: the
        # attempt is swallowed into truth_error, and the JSONL still records
        # everything, including that the row did not land.
        truth_writer=write_incident,
    )

    if record is None:
        print(f"reverted {args.fault} on {args.target}")
    else:
        print(
            f"injected {args.fault} on {args.target} "
            f"(incident_id={record['incident_id']})"
        )
        deployed = record["correlated_deploy"]
        if deployed:
            print(
                f"correlated deploy: {deployed['service']} {deployed['version']} "
                f"at {deployed['deployed_at']} - \"{deployed['message']}\""
            )
        elif record["deploy_error"]:
            print(f"correlated deploy NOT written: {record['deploy_error']}")
        else:
            print("no correlated deploy (this fault has no deploy to blame)")
        if record["truth_error"]:
            print(f"ground truth NOT written to postgres: {record['truth_error']}")
        if record["ended_at"]:
            print(f"reverted after {args.duration}s")
        else:
            print("fault left active; re-run with --clear to revert")


if __name__ == "__main__":
    main()

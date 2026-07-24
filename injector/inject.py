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
import json
import os
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

import httpx

# Host ports as mapped in docker-compose.yml.
TARGET_PORTS = {
    "api-gateway": 8001,
    "data-service": 8002,
    "downstream-dep": 8003,
}

GROUND_TRUTH_PATH = Path(__file__).with_name("ground_truth.jsonl")


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
) -> dict | None:
    """Orchestrate an injection. Returns the ground-truth record, or None.

    - clear (or duration == 0): disable the fault only; no ground truth.
    - duration > 0: enable, wait, disable; full ground-truth window.
    - duration is None: enable and leave active; ground truth with ended_at=None.
    """
    url = resolve_target(target)

    if clear or duration == 0:
        poster(url, {"fault": fault, "enabled": False, "params": params})
        return None

    poster(url, {"fault": fault, "enabled": True, "params": params})
    started_at = now()
    ended_at: str | None = None

    if duration is not None:
        sleeper(duration)
        poster(url, {"fault": fault, "enabled": False, "params": params})
        ended_at = now()

    record = {
        "incident_id": str(uuid.uuid4()),
        "fault": fault,
        "target": target,
        "params": params,
        "started_at": started_at,
        "ended_at": ended_at,
    }
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
    )

    if record is None:
        print(f"reverted {args.fault} on {args.target}")
    else:
        print(
            f"injected {args.fault} on {args.target} "
            f"(incident_id={record['incident_id']})"
        )
        if record["ended_at"]:
            print(f"reverted after {args.duration}s")
        else:
            print("fault left active; re-run with --clear to revert")


if __name__ == "__main__":
    main()

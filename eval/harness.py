"""The live orchestration: one scenario in, one RunRecord out.

Every collaborator is injected with a real default, so production needs no
wiring and the tests start no container and call no model - the same discipline
`build_graph` uses for `run`/`call`/`act_on`.

The sequence encodes the traps HANDOFF records losing runs to, so they cannot be
forgotten again. They are behaviour here, not prose in a runbook.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from pathlib import Path

from pydantic import BaseModel

from agent import config
from agent.graph.render import summarise_alert
from agent.tools.base import utc_now
from eval.score import RunRecord

SCENARIO_DIR = Path(__file__).resolve().parent / "scenarios"
# One append-only JSONL per suite. Already gitignored.
DEFAULT_RESULTS_DIR = Path(__file__).resolve().parent / "results"

# Roughly how long one run occupies the stack end to end: the fault clear, the
# reseed, the Prometheus restart and its scrape, the baseline, the warmup, the
# investigation and the wind-down. A fault must expire well inside it, or it is
# still active when the next run seeds its ledger.
CYCLE_SECONDS = 360

# How long each fault is held. Shorter than CYCLE_SECONDS, and long enough that
# a whole investigation fits inside the window (see `run_outlived_fault`).
FAULT_SECONDS = 180

# What one investigation has actually cost: six live runs came to roughly $0.85
# on Sonnet 4.5. A measured figure rather than a guess, and the only honest
# basis for telling someone what a suite will cost before they start it.
OBSERVED_COST_PER_RUN_USD = 0.14

# Metrics only move when requests flow, so traffic runs before anything is
# measured. The baseline gives the rate queries something to compare against.
BASELINE_SECONDS = 120
# The 60s rate window has to sit entirely inside the fault, or the rate is an
# average of healthy and faulted minutes.
WARMUP_SECONDS = 100
# What the traffic generator is sized for. Traffic MUST outlive the
# investigation: if it stops mid-run, rate(http_requests_total) goes to zero on
# all three services, which is the very signal the timeout diagnosis turns on.
# Measuring through that lens would publish a harness bug as an agent finding.
MAX_INVESTIGATION_SECONDS = 240
TRAFFIC_MARGIN_SECONDS = 30

# Prometheus answers on its port long before it holds any samples, so it is
# polled rather than slept on. At a 15s scrape interval these allow ~90s.
PROMETHEUS_PROBE_ATTEMPTS = 18
PROMETHEUS_PROBE_INTERVAL_SECONDS = 5


class Scenario(BaseModel):
    """One scenario: the alert to investigate, and the fault to inject first.

    `params` and `deploy` are pinned rather than defaulted. Different params are
    different severities, so repeats with different ones are not repeats; and a
    deploy decision left to DEPLOY_PROBABILITY is a coin flip that makes the
    deploy-correlation metric meaningless at three runs per scenario.
    """

    name: str
    alert_path: Path
    fault: str
    target: str
    params: dict = {}
    deploy: bool = True
    fault_duration_seconds: int = FAULT_SECONDS


def _scenario(name: str, **kw) -> Scenario:
    return Scenario(name=name, alert_path=SCENARIO_DIR / f"{name}.json", **kw)


# -- the real stack -----------------------------------------------------
#
# Every one of these is a default, never a hard-wired call: the tests pass fakes
# and so start no container, spend no money and touch no database. Imports of
# heavy or optional things stay inside the functions, so `import eval.harness`
# needs neither Docker nor a DSN - which is what keeps `cd eval && pytest` green
# with no credentials.

TRAFFIC_TARGET = "api-gateway"
TRAFFIC_RPS = 5
SEED_NOISE = 6
SEED_HOURS = 2
EXPECTED_CONTAINERS = ("api-gateway", "data-service", "downstream-dep", "prometheus")


def clear_every_fault() -> None:
    """Best effort, on every victim service.

    Never raises: this runs in the suite's `finally`, where the interesting
    error is the one already on its way out.
    """
    import httpx

    from injector.inject import TARGET_PORTS, resolve_target

    for target in TARGET_PORTS:
        try:
            httpx.delete(f"{resolve_target(target)}/admin/fault", timeout=5.0)
        except Exception as exc:  # noqa: BLE001
            print(f"warning: could not clear the fault on {target}: {exc}")


def reseed_ledger() -> list[dict]:
    """Clear and reseed the deploy ledger, returning what was written."""
    from injector.seed_deploys import clear_deploys, seed, write_deploys

    return seed(
        noise=SEED_NOISE,
        hours=SEED_HOURS,
        clear=True,
        writer=write_deploys,
        clearer=clear_deploys,
    )


def _compose(*args: str) -> int:
    import subprocess

    from agent import config

    completed = subprocess.run(
        ["docker", "compose", "-p", config.COMPOSE_PROJECT, *args],
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        print(f"warning: docker compose {' '.join(args)}: {completed.stderr.strip()}")
    return completed.returncode


def recreate_prometheus() -> None:
    """Recreate the container, rather than restarting it.

    `restart` does NOT wipe the TSDB - there is no volume, but the process keeps
    its samples - so the previous run's fault bleeds into this run's queries.
    """
    _compose("rm", "-sf", "prometheus")
    _compose("up", "-d", "prometheus")


def prometheus_has_scraped() -> bool:
    """True once at least one target is up and has been scraped."""
    import httpx

    from agent import config

    try:
        response = httpx.get(
            f"{config.PROMETHEUS_URL}/api/v1/query",
            params={"query": "up"},
            timeout=5.0,
        )
        result = response.json().get("data", {}).get("result", [])
    except Exception:  # noqa: BLE001 - not ready yet is the expected answer
        return False
    return any(sample.get("value", [None, "0"])[1] == "1" for sample in result)


def containers_are_up() -> bool:
    """A host suspend leaves everything exited(255), and voids a paid run."""
    import subprocess

    from agent import config

    completed = subprocess.run(
        ["docker", "compose", "-p", config.COMPOSE_PROJECT, "ps",
         "--services", "--filter", "status=running"],
        capture_output=True,
        text=True,
    )
    running = set(completed.stdout.split())
    missing = [name for name in EXPECTED_CONTAINERS if name not in running]
    if missing:
        print(f"warning: containers not running: {', '.join(missing)}")
    return not missing


def start_traffic_task(*, duration: int):
    """Start the traffic generator alongside the run, and return its handle.

    An asyncio task rather than a thread, so it can be cancelled deterministically
    once the investigation returns. Its requests are synchronous, but they are
    local and sub-millisecond next to a model call.
    """
    from injector import traffic

    return asyncio.ensure_future(
        traffic.run(target=TRAFFIC_TARGET, rps=TRAFFIC_RPS, duration=duration)
    )


def stop_traffic_task(handle) -> None:
    handle.cancel()


async def inject_fault(scenario: Scenario, *, incident_id: str, payload: dict | None = None):
    """Hold the fault for its duration, off the event loop.

    `inject.run` sleeps synchronously for the whole fault window, which would
    block the investigation running concurrently with it.
    """
    from injector.inject import (
        GROUND_TRUTH_PATH,
        run as inject_run,
        write_incident,
    )

    return await asyncio.to_thread(
        inject_run,
        fault=scenario.fault,
        target=scenario.target,
        params=dict(scenario.params),
        duration=scenario.fault_duration_seconds,
        clear=False,
        poster=_default_fault_poster,
        sleeper=_blocking_sleep,
        gt_path=GROUND_TRUTH_PATH,
        now=_injector_now,
        deploy=scenario.deploy,
        incident_id=incident_id,
        # The incidents row the investigations FK points at, carrying the
        # scenario webhook as raw_payload.
        truth_writer=write_incident,
        truth_payload=payload,
    )


def _default_fault_poster(url: str, payload: dict) -> None:
    from injector.inject import _default_poster

    _default_poster(url, payload)


def _blocking_sleep(seconds: float) -> None:
    import time

    time.sleep(seconds)


def _injector_now() -> str:
    from injector.inject import _utc_now_iso

    return _utc_now_iso()


async def investigate_alert(alert, *, incident_id: str):
    from uuid import UUID

    from agent.graph import investigate

    return await investigate(alert, incident_id=UUID(incident_id))


def flush_traces() -> None:
    from agent.graph import trace

    trace.flush()


def append_run(path: Path):
    """A `persist` that appends one JSON line per run.

    The same idiom as `write_ground_truth`, and for the same reasons: a crash at
    run 9 keeps runs 1-8, and `--replay` reads back exactly the file the live
    suite wrote, so replay cannot silently diverge from live.
    """

    def persist(record: RunRecord) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record.model_dump(mode="json")) + "\n")

    return persist


def load_results(path: Path) -> list[RunRecord]:
    """Read back what `append_run` wrote.

    The exact inverse, reading the exact same file, so `--replay` cannot drift
    away from what a live suite produced. Torn trailing lines are skipped for
    the same reason `load_ground_truth` skips them: an interrupted suite leaves
    one, and it must not cost the runs behind it.
    """
    records: list[RunRecord] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        records.append(RunRecord.model_validate(payload))
    return records


def _discard(record: RunRecord) -> None:
    """The default `persist`: the suite supplies a real one with its own file."""


def traffic_duration_seconds() -> int:
    """Computed, never a constant: traffic has to outlive the investigation."""
    return (
        BASELINE_SECONDS
        + WARMUP_SECONDS
        + MAX_INVESTIGATION_SECONDS
        + TRAFFIC_MARGIN_SECONDS
    )


# How many tool calls the opening sweep makes. Those are the ones whose health
# decides whether the investigation could see anything at all.
OPENING_SWEEP_CALLS = 4


def evidence_results(report) -> list[dict]:
    """The tool results from a report, or nothing.

    The single place that knows the shape `finalize` writes into the evidence
    jsonb. It lives here rather than in `score.py` so the pure core stays
    uncoupled from a serialization detail - and so there is one place to fix
    when that shape changes.
    """
    if report is None:
        return []
    return [
        entry.get("result", {}) for entry in report.evidence.get("results", [])
    ]


def sweep_was_healthy(results: list[dict]) -> bool:
    """Could the opening sweep see anything at all?

    Every call has to have succeeded, and the metric calls have to have found at
    least one series. Empty is NOT failure anywhere else: "no deploys in the
    window" excludes a code change, and flat traffic is the evidence under a
    timeout fault. But zero *series* is not flat traffic - it is a Prometheus
    that has not scraped yet, and an investigation that started blind.
    """
    sweep = results[:OPENING_SWEEP_CALLS]
    if not sweep:
        return False
    for result in sweep:
        if not result.get("ok", False):
            return False
        if "series" in result and not result["series"]:
            return False
    return True


def _void(scenario, repeat_index, incident_id, reason, *, persist, flush) -> RunRecord:
    """Record a run that never had a fair chance, and keep it in the results.

    Persisted and flushed like any other: a suite that quietly drops its void
    runs cannot report how much of it was thrown away.
    """
    record = RunRecord(
        scenario=scenario.name,
        repeat_index=repeat_index,
        incident_id=incident_id,
        void_reason=reason,
    )
    persist(record)
    flush()
    return record


async def run_one(
    scenario: Scenario,
    *,
    clear_faults=clear_every_fault,
    seed=reseed_ledger,
    restart_prometheus=recreate_prometheus,
    probe_prometheus=prometheus_has_scraped,
    check_containers=containers_are_up,
    start_traffic=start_traffic_task,
    stop_traffic=stop_traffic_task,
    inject=inject_fault,
    investigate=investigate_alert,
    persist=_discard,
    flush=flush_traces,
    sleeper=asyncio.sleep,
    now=utc_now,
    repeat_index: int = 0,
) -> RunRecord:
    """Run one scenario end to end and return what happened.

    Every step below closes a way a paid run has actually been lost:

    1. clear every fault - a leaked fault from a killed run contaminates this one
    2. reseed the ledger - the seeder APPENDS, so re-running doubles the noise,
       and the ledger decays (SWEEP_DEPLOYS_MINUTES is 120), so it is per run
    3. recreate Prometheus - `restart` does not wipe the TSDB; the previous
       fault bleeds into this run's queries
    4. poll until it has scraped - `sleep 5` is not enough at a 15s interval,
       and the opening sweep then finds no series at all
    5. verify the containers are up - a host suspend left everything exited(255)
       and voided a paid run
    6. start traffic, wait the baseline
    7. inject in the background, wait the warmup
    8. investigate, with the pre-minted incident id the FK needs
    9. stop traffic only once the investigation has returned
    10. await the injector, so the fault is really reverted and its row written
    """
    payload = json.loads(scenario.alert_path.read_text(encoding="utf-8"))
    alert = summarise_alert(payload)
    incident_id = str(uuid.uuid4())

    clear_faults()
    # The seeder returns what it wrote: the only place the noise shas are known,
    # and what lets a report be caught blaming a deploy that caused nothing.
    seeded = seed() or []
    restart_prometheus()
    ready = False
    for attempt in range(PROMETHEUS_PROBE_ATTEMPTS):
        if probe_prometheus():
            ready = True
            break
        await sleeper(PROMETHEUS_PROBE_INTERVAL_SECONDS)

    # Void before spending anything: a run against a stack that is not answering
    # costs real money to learn nothing about the agent.
    if not ready:
        return _void(scenario, repeat_index, incident_id, "prometheus_never_ready",
                     persist=persist, flush=flush)
    if not check_containers():
        return _void(scenario, repeat_index, incident_id, "containers_down",
                     persist=persist, flush=flush)

    traffic = start_traffic(duration=traffic_duration_seconds())
    await sleeper(BASELINE_SECONDS)

    injection = asyncio.ensure_future(
        inject(scenario, incident_id=incident_id, payload=payload)
    )
    await sleeper(WARMUP_SECONDS)

    report = None
    started_at = now()
    ended_at = None
    failure: str | None = None
    try:
        report = await investigate(alert, incident_id=incident_id)
        ended_at = now()
    except Exception as exc:  # noqa: BLE001 - a failed run is data, not an abort
        failure = f"run_failed: {exc}"
    finally:
        # All of this has to happen even on the way out of a failure: a traffic
        # generator left running poisons the next run, an injector never awaited
        # never reverts its fault, and spans are posted from a background thread
        # - so a process that stops without flushing loses the trace it paid
        # for. The trace of the run that failed is the one worth reading.
        stop_traffic(traffic)
        await injection
        results = evidence_results(report)
        record = RunRecord(
            scenario=scenario.name,
            repeat_index=repeat_index,
            incident_id=incident_id,
            report=report,
            started_at=started_at,
            ended_at=ended_at or now(),
            # Collected here, so the scorer never reaches into the jsonb.
            known_queries=[r["query"] for r in results if r.get("query")],
            sweep_healthy=sweep_was_healthy(results) if report is not None else None,
            seeded_commit_shas=[
                row["commit_sha"] for row in seeded if row.get("commit_sha")
            ],
            writes_enabled=config.AGENT_ALLOW_WRITES,
            # `action_taken` is filled only when an action really ran: a dry
            # run, a failed action and a timeout all leave it null.
            executed=report is not None and report.action_taken is not None,
            # A crashed run is void - there is nothing to score - but it is kept
            # and counted rather than dropped. The reason travels with it.
            void_reason=failure,
        )
        # Persisted per run, not per suite: a crash at run 9 must not discard
        # the seventy minutes of paid runs behind it.
        persist(record)
        flush()

    return record


async def run_suite(
    scenarios: list[Scenario],
    *,
    repeat: int = 3,
    allow_writes: bool = False,
    clear_faults=clear_every_fault,
    **collaborators,
) -> list[RunRecord]:
    """Run every scenario `repeat` times and return the records, in order.

    Writes are off unless asked for. The gate reaches the same verdict either
    way - which is the documented reason a dry suite across every fault is worth
    running - and the flag is restored in a `finally`, because it belongs to
    this suite rather than to the rest of the process. Same idiom as
    `agent.graph.run.main`.
    """
    previous_writes = config.AGENT_ALLOW_WRITES
    config.AGENT_ALLOW_WRITES = allow_writes
    records: list[RunRecord] = []
    try:
        for index in range(repeat):
            for scenario in scenarios:
                records.append(
                    await run_one(
                        scenario,
                        repeat_index=index,
                        clear_faults=clear_faults,
                        **collaborators,
                    )
                )
    finally:
        config.AGENT_ALLOW_WRITES = previous_writes
        # A fault lives in the victim service's memory, not in this process, so
        # an interrupted suite leaves it running and contaminates whatever runs
        # next - possibly in another session entirely.
        clear_faults()
    return records


SCENARIOS: dict[str, Scenario] = {
    s.name: s
    for s in (
        _scenario(
            "gateway-timeout",
            fault="timeout",
            target="api-gateway",
            params={},
            deploy=True,
        ),
        _scenario(
            "data-service-bad-config",
            fault="bad_config",
            target="data-service",
            params={},
            # The one run with no causal deploy, so a report that blames one
            # anyway is caught. Without it `spurious_deploy_blame` has no
            # denominator at all.
            deploy=False,
        ),
        _scenario(
            "downstream-latency",
            fault="latency",
            target="downstream-dep",
            params={"delay_ms": 3000},
            deploy=True,
        ),
        _scenario(
            "downstream-memory",
            fault="memory",
            target="downstream-dep",
            params={"bytes": 10485760},
            deploy=True,
        ),
    )
}

import asyncio
import contextlib
import importlib
import inspect
import json
from datetime import datetime, timedelta, timezone

from agent import config
from agent.graph.state import InvestigationReport
from eval.score import RunRecord
from eval.harness import (
    append_run,
    BASELINE_SECONDS,
    MAX_INVESTIGATION_SECONDS,
    SCENARIOS,
    WARMUP_SECONDS,
    run_one,
    run_suite,
)


T0 = datetime(2026, 9, 14, 11, 0, tzinfo=timezone.utc)

class Stack:
    """A fake stack that records what the harness asked it to do, in order."""

    def __init__(self, *, report=None, investigate_seconds=30, probes_until_ready=1):
        self.probe_attempts = 0
        self.probes_until_ready = probes_until_ready
        self.containers_up = True
        self.persisted: list = []
        self.seeded_rows = [{"commit_sha": "aaa1111"}, {"commit_sha": "bbb2222"}]
        self.calls: list[str] = []
        self.report = report or InvestigationReport(
            diagnosis="downstream-dep is slow",
            fault_type="latency",
            service="downstream-dep",
            confidence=0.8,
        )
        self.investigate_seconds = investigate_seconds
        self.clock = T0
        self.traffic_duration: int | None = None
        # Set when the injected fault reverts. The investigation runs while the
        # injector is still sleeping, which is the whole reason the incident id
        # has to be minted by the caller.
        self.fault_over = asyncio.Event()

    # -- the clock the harness reads, advanced by its own sleeps --
    def now(self):
        return self.clock

    async def sleeper(self, seconds):
        self.calls.append(f"sleep:{int(seconds)}")
        self.clock += timedelta(seconds=seconds)
        await asyncio.sleep(0)  # yield, so a backgrounded injector can run

    # -- stack control --
    def clear_faults(self):
        self.calls.append("clear_faults")

    def seed(self):
        self.calls.append("seed")
        return self.seeded_rows

    def restart_prometheus(self):
        self.calls.append("restart_prometheus")

    def probe_prometheus(self):
        self.calls.append("probe_prometheus")
        self.probe_attempts += 1
        return self.probe_attempts >= self.probes_until_ready

    def check_containers(self):
        self.calls.append("check_containers")
        return self.containers_up

    def start_traffic(self, *, duration):
        self.calls.append("start_traffic")
        self.traffic_duration = duration
        return "traffic-handle"

    def stop_traffic(self, handle):
        self.calls.append(f"stop_traffic:{handle}")
        self.traffic_stopped_at = self.clock

    async def inject(self, scenario, *, incident_id, payload=None):
        self.calls.append("inject")
        self.incident_id_seen_by_injector = incident_id
        self.payload_seen_by_injector = payload
        await self.fault_over.wait()
        self.calls.append("inject_returned")

    async def investigate(self, alert, *, incident_id):
        self.calls.append("investigate")
        self.incident_id_seen_by_investigation = incident_id
        self.clock += timedelta(seconds=self.investigate_seconds)
        self.fault_over.set()  # the injector's sleep expires during the run
        return self.report

    async def exploding_investigate(self, alert, *, incident_id):
        self.calls.append("investigate")
        # The real injector reverts on its own timer whatever the agent does, so
        # the fake must too - otherwise awaiting it would hang rather than test.
        self.fault_over.set()
        raise RuntimeError("the model call blew up")

    def persist(self, record):
        self.calls.append("persist")
        self.persisted.append(record)

    def flush(self):
        self.calls.append("flush")


def drive(stack, scenario="downstream-latency", **kw):
    return asyncio.run(
        run_one(
            SCENARIOS[scenario],
            clear_faults=stack.clear_faults,
            seed=stack.seed,
            restart_prometheus=stack.restart_prometheus,
            probe_prometheus=stack.probe_prometheus,
            check_containers=stack.check_containers,
            start_traffic=stack.start_traffic,
            stop_traffic=stack.stop_traffic,
            inject=stack.inject,
            investigate=stack.investigate,
            persist=stack.persist,
            flush=stack.flush,
            sleeper=stack.sleeper,
            now=stack.now,
            **kw,
        )
    )


def test_one_run_calls_the_steps_in_order():
    """Each step closes a documented way to lose a paid run, and several of them
    only work in this order: the ledger decays, so it is seeded per run; the
    TSDB is wiped before the fault rather than after; traffic starts before the
    baseline because metrics only move when requests flow."""
    stack = Stack()

    record = drive(stack)

    steps = [c for c in stack.calls if not c.startswith("sleep:")]
    assert steps == [
        "clear_faults",
        "seed",
        "restart_prometheus",
        "probe_prometheus",
        "check_containers",
        "start_traffic",
        "inject",
        "investigate",
        # Traffic is stopped only once the investigation has returned, and the
        # injector is awaited only after that - a backgrounded injector killed
        # with its shell never reverts the fault and never writes its row.
        "stop_traffic:traffic-handle",
        "inject_returned",
        "persist",
        "flush",
    ]

    assert record.scenario == "downstream-latency"
    assert record.report is stack.report
    assert record.void_reason is None


def test_the_incident_id_is_the_same_one_the_injector_and_the_investigation_saw():
    """inject.run() mints its id inside the record dict, AFTER its sleep returns
    - but the investigation runs while the injector is still sleeping, and the
    investigations FK needs that id now. So the caller mints it and both sides
    are told, rather than one side reading it back from the other."""
    stack = Stack()

    record = drive(stack)

    assert record.incident_id
    assert stack.incident_id_seen_by_injector == record.incident_id
    assert stack.incident_id_seen_by_investigation == record.incident_id

    # The scenario webhook goes with it: the incidents row's raw_payload column
    # holds an Alertmanager webhook on every other row, and the harness is the
    # only place that has already loaded it.
    assert stack.payload_seen_by_injector["commonLabels"]["service"]


def test_each_run_gets_its_own_incident_id():
    first = drive(Stack())
    second = drive(Stack())

    assert first.incident_id != second.incident_id


def test_traffic_outlives_the_investigation():
    """If traffic stops during the run, rate(http_requests_total) falls to zero
    on all three services - which is exactly the discriminator the timeout
    diagnosis turns on. We have chosen to MEASURE that miss, not fix it, so we
    must not measure it through a lens the harness broke."""
    slow = Stack(investigate_seconds=MAX_INVESTIGATION_SECONDS * 3)

    record = drive(slow)

    assert slow.traffic_stopped_at >= record.ended_at > record.started_at
    assert slow.calls.index("stop_traffic:traffic-handle") > slow.calls.index(
        "investigate"
    )

    # And the generator was sized for a long run rather than a nominal one.
    assert slow.traffic_duration > BASELINE_SECONDS + WARMUP_SECONDS
    assert slow.traffic_duration >= (
        BASELINE_SECONDS + WARMUP_SECONDS + MAX_INVESTIGATION_SECONDS
    )


def test_the_harness_waits_for_prometheus_before_the_baseline():
    """A fixed `sleep 5` is not enough at a 15s scrape interval: the container
    answers on its HTTP port well before it holds any samples, and the opening
    sweep then finds no series at all. So it is polled, not slept on."""
    slow_prometheus = Stack(probes_until_ready=4)

    drive(slow_prometheus)

    assert slow_prometheus.probe_attempts == 4
    assert slow_prometheus.calls.index("start_traffic") > max(
        i for i, c in enumerate(slow_prometheus.calls) if c == "probe_prometheus"
    )


def test_a_prometheus_that_never_becomes_ready_voids_the_run():
    """Investigating a Prometheus with no samples costs real money to learn
    nothing about the agent. Better to void the run before paying for it."""
    dead = Stack(probes_until_ready=10_000)

    record = drive(dead)

    assert record.void_reason == "prometheus_never_ready"
    assert "investigate" not in dead.calls
    assert "start_traffic" not in dead.calls
    # Still recorded and still flushed - a void run is data too.
    assert dead.calls[-2:] == ["persist", "flush"]


def test_containers_that_are_down_void_the_run():
    """A host suspend left everything exited(255) and voided a paid run."""
    suspended = Stack()
    suspended.containers_up = False

    record = drive(suspended)

    assert record.void_reason == "containers_down"
    assert "investigate" not in suspended.calls


def drive_suite(stack, scenarios=("downstream-latency",), repeat=1, **kw):
    return asyncio.run(
        run_suite(
            [SCENARIOS[name] for name in scenarios],
            repeat=repeat,
            clear_faults=stack.clear_faults,
            seed=stack.seed,
            restart_prometheus=stack.restart_prometheus,
            probe_prometheus=stack.probe_prometheus,
            check_containers=stack.check_containers,
            start_traffic=stack.start_traffic,
            stop_traffic=stack.stop_traffic,
            inject=stack.inject,
            investigate=stack.investigate,
            persist=stack.persist,
            flush=stack.flush,
            sleeper=stack.sleeper,
            now=stack.now,
            **kw,
        )
    )


def test_the_suite_runs_dry_by_default_and_restores_the_flag():
    """The gate reaches the same verdict either way - which is the documented
    reason a dry suite across every fault is worth running - so writes stay off
    unless asked for. The flag is for this suite, not for the process."""
    config.AGENT_ALLOW_WRITES = False
    stack = Stack()

    records = drive_suite(stack)

    assert records[0].writes_enabled is False
    assert records[0].executed is False
    assert config.AGENT_ALLOW_WRITES is False


def test_allowing_writes_is_restored_even_when_a_run_raises():
    config.AGENT_ALLOW_WRITES = False
    stack = Stack()
    stack.investigate = stack.exploding_investigate

    # Whether the failure propagates is a separate question (see the suite's
    # error handling); the flag must be restored either way.
    with contextlib.suppress(RuntimeError):
        drive_suite(stack, allow_writes=True)

    assert config.AGENT_ALLOW_WRITES is False


def test_an_executed_action_is_recorded_on_the_run():
    """`action_taken` is filled only when something really ran - a dry run, a
    failed action and a timeout all leave it null."""
    acted = InvestigationReport(
        diagnosis="downstream-dep leaked memory",
        fault_type="memory",
        service="downstream-dep",
        confidence=0.9,
        action_taken="restart_service(downstream-dep)",
        action_result="restarted downstream-dep",
    )
    stack = Stack(report=acted)

    records = drive_suite(stack, allow_writes=True)

    assert records[0].writes_enabled is True
    assert records[0].executed is True


def test_the_trace_is_flushed_even_when_the_investigation_raises():
    """Spans are posted from a background thread, so a process that stops
    without flushing loses the trace it just paid for - and the trace of the run
    that FAILED is the one most worth reading. The traffic and the injector are
    in the same finally: a generator left running poisons the next run, and a
    backgrounded injector never awaited never reverts its fault."""
    stack = Stack()
    stack.investigate = stack.exploding_investigate

    with contextlib.suppress(RuntimeError):
        drive(stack)

    assert "flush" in stack.calls
    assert "stop_traffic:traffic-handle" in stack.calls
    assert "inject_returned" in stack.calls
    assert "persist" in stack.calls


def test_a_failed_run_does_not_abort_the_suite():
    """Twelve runs is over an hour and a couple of dollars. One that dies on a
    transient API error must not take the rest with it - it is recorded as a
    failure and the suite carries on, because a failure is data."""
    stack = Stack()
    stack.investigate = stack.exploding_investigate

    records = drive_suite(stack, scenarios=("downstream-latency",), repeat=3)

    assert len(records) == 3
    # The reason travels with the record, message and all: a suite of failures
    # that all say "run_failed" tells you nothing about why.
    assert all(r.void_reason.startswith("run_failed") for r in records)
    assert all("the model call blew up" in r.void_reason for r in records)
    assert all(r.report is None for r in records)
    assert stack.calls.count("flush") == 3


def test_an_interrupt_stops_the_suite_rather_than_being_recorded():
    """Ctrl-C means stop, not 'record a failure and keep spending'."""

    async def interrupted(alert, *, incident_id):
        stack.calls.append("investigate")
        stack.fault_over.set()
        raise KeyboardInterrupt

    stack = Stack()
    stack.investigate = interrupted

    try:
        drive_suite(stack, repeat=3)
    except KeyboardInterrupt:
        pass
    else:  # pragma: no cover - the assertion below is the real failure message
        raise AssertionError("the interrupt was swallowed")

    assert stack.calls.count("investigate") == 1


def test_each_run_is_persisted_before_the_next_one_starts():
    """Twelve runs is seventy minutes and about two dollars. Batching the write
    to the end means an interrupt at run 9 throws all of it away, so the results
    file is appended per run - the same idiom as write_ground_truth."""
    stack = Stack()

    drive_suite(stack, repeat=3)

    persists = [i for i, c in enumerate(stack.calls) if c == "persist"]
    investigates = [i for i, c in enumerate(stack.calls) if c == "investigate"]

    assert len(persists) == 3
    # Every run's write lands before the next run's investigation begins.
    assert persists[0] < investigates[1] < persists[1] < investigates[2]


def test_an_interrupt_keeps_the_runs_that_already_finished():
    stack = Stack()
    finished: list = []

    async def investigate_then_stop(alert, *, incident_id):
        stack.calls.append("investigate")
        stack.fault_over.set()
        if len(finished) == 2:
            raise KeyboardInterrupt
        finished.append(incident_id)
        stack.clock += timedelta(seconds=30)
        return stack.report

    stack.investigate = investigate_then_stop

    with contextlib.suppress(KeyboardInterrupt):
        drive_suite(stack, repeat=5)

    assert len(stack.persisted) == 3  # two completed, plus the interrupted one
    assert [r.report is not None for r in stack.persisted] == [True, True, False]


def test_an_aborted_suite_still_clears_every_fault():
    """A fault lives in the victim service's memory, not in this process, so an
    interrupted suite leaves it running - and the next session's first run is
    contaminated before it starts. The injector normally reverts it; an abort is
    exactly the case where it did not get to."""
    stack = Stack()

    async def interrupted(alert, *, incident_id):
        stack.calls.append("investigate")
        stack.fault_over.set()
        raise KeyboardInterrupt

    stack.investigate = interrupted

    with contextlib.suppress(KeyboardInterrupt):
        drive_suite(stack, repeat=3)

    # The last thing the suite does, after the interrupt has begun unwinding.
    assert stack.calls[-1] == "clear_faults"


def evidence(*entries):
    return {"results": [{"iteration": 0, "result": e} for e in entries]}


def metrics_result(*, ok=True, series=(), query="rate(http_requests_total[1m])"):
    return {
        "tool": "query_metrics", "ok": ok, "summary": "s", "source": "prometheus",
        "query": query, "series": list(series),
    }


def logs_result(*, ok=True, query="logs(ERROR, 15m)"):
    return {
        "tool": "query_logs", "ok": ok, "summary": "s", "source": "docker",
        "query": query, "lines": [],
    }


def deploys_result(*, ok=True, query="deploys(all, 120m)", deploys=()):
    return {
        "tool": "query_deploy_history", "ok": ok, "summary": "s", "source": "postgres",
        "query": query, "deploys": list(deploys),
    }


def test_the_harness_collects_the_queries_the_tools_actually_issued():
    """The scorer resolves citations against these. It must not dig them out of
    the evidence jsonb itself - that is a deep untyped reach into a blob whose
    shape belongs to `finalize`, and it would couple the pure core to a
    serialization detail."""
    report = InvestigationReport(
        diagnosis="d", fault_type="latency", service="downstream-dep",
        evidence=evidence(metrics_result(), logs_result(), deploys_result()),
    )
    stack = Stack(report=report)

    record = drive(stack)

    assert record.known_queries == [
        "rate(http_requests_total[1m])",
        "logs(ERROR, 15m)",
        "deploys(all, 120m)",
    ]


def test_an_empty_log_or_deploy_result_is_still_a_healthy_sweep():
    """'No deploys in the window' EXCLUDES a code change, and flat traffic IS
    the evidence under a timeout fault. Voiding a run for an empty result would
    void the very scenario this suite exists to measure."""
    report = InvestigationReport(
        diagnosis="d", fault_type="timeout", service="api-gateway",
        evidence=evidence(
            metrics_result(series=[{"labels": {}, "points": []}]),
            logs_result(),
            deploys_result(deploys=[]),
        ),
    )

    record = drive(Stack(report=report))

    assert record.sweep_healthy is True


def test_a_metric_query_that_found_no_series_at_all_is_an_unhealthy_sweep():
    """Zero series is not flat traffic - it is a Prometheus that has not
    scraped, and an investigation that started blind."""
    blind = InvestigationReport(
        diagnosis="d", fault_type="unknown",
        evidence=evidence(metrics_result(series=[]), logs_result()),
    )

    assert drive(Stack(report=blind)).sweep_healthy is False


def test_a_failed_tool_call_is_an_unhealthy_sweep():
    broken = InvestigationReport(
        diagnosis="d", fault_type="unknown",
        evidence=evidence(metrics_result(series=[{"labels": {}}]), logs_result(ok=False)),
    )

    assert drive(Stack(report=broken)).sweep_healthy is False


def test_the_harness_records_the_shas_it_seeded_the_ledger_with():
    """spurious_deploy_blame needs something to recognise a false accusation
    against. The seeder knows what it wrote; nothing downstream does."""
    stack = Stack()

    record = drive(stack)

    assert record.seeded_commit_shas == ["aaa1111", "bbb2222"]


def test_every_collaborator_has_a_real_default():
    """Production wires nothing: the defaults are the live stack, and the tests
    override them. Same discipline as build_graph's run/call/act_on."""
    parameters = inspect.signature(run_one).parameters

    collaborators = [
        "clear_faults", "seed", "restart_prometheus", "probe_prometheus",
        "check_containers", "start_traffic", "stop_traffic", "inject",
        "investigate", "persist", "flush", "sleeper", "now",
    ]
    for name in collaborators:
        assert parameters[name].default is not inspect.Parameter.empty, name
        assert callable(parameters[name].default), name


def test_importing_the_harness_needs_no_docker_and_no_database(monkeypatch):
    """`cd eval && pytest` has to stay green with nothing configured, so the
    heavy imports live inside the functions that need them."""
    monkeypatch.delenv("DATABASE_URL", raising=False)

    importlib.reload(importlib.import_module("eval.harness"))


def test_the_default_persist_appends_one_json_line_per_run(tmp_path):
    """Append-only, like write_ground_truth: a crash at run 9 keeps runs 1-8,
    and --replay reads back exactly what the live suite wrote."""
    path = tmp_path / "suite.jsonl"
    persist = append_run(path)

    persist(RunRecord(scenario="gateway-timeout", repeat_index=0))
    persist(RunRecord(scenario="gateway-timeout", repeat_index=1))

    lines = path.read_text(encoding="utf-8").strip().splitlines()
    assert [json.loads(line)["repeat_index"] for line in lines] == [0, 1]


def test_each_repeat_reseeds_the_ledger_before_its_own_injection():
    """Per run, not per suite: the ledger decays out of the sweep's 120-minute
    window, so run 12 would find nothing. And the seeder APPENDS - seeding once
    and running twelve times would give the last run twelve times the noise
    density of the first. Never after the injection: the correlated deploy has
    to be the newest thing in the ledger."""
    stack = Stack()

    drive_suite(stack, repeat=2)

    ordered = [
        c for c in stack.calls if c in ("clear_faults", "seed", "inject", "investigate")
    ]

    assert ordered == [
        "clear_faults", "seed", "inject", "investigate",
        "clear_faults", "seed", "inject", "investigate",
        "clear_faults",  # the suite's own cleanup
    ]


def test_a_completed_suite_leaves_no_fault_behind_either():
    stack = Stack()

    drive_suite(stack, repeat=2)

    assert stack.calls[-1] == "clear_faults"

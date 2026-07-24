# HANDOFF.md — Session State

This file carries context between Claude Code sessions. Update it at the end of every session. Read it (alongside CLAUDE.md) at the start of every session.

---

## Last Session

**Date:** Day 3–7 (Phase 1)
**What was done:**
- Built all three victim services (api-gateway, data-service, downstream-dep) via TDD. Each has: `/health`, `/metrics`, `/admin/fault` (POST enable/disable + DELETE clear), JSON stdout logging, `X-Trace-Id` propagation middleware, per-service Prometheus metrics, `requirements.txt`, and a `python:3.12-slim` Dockerfile.
  - **api-gateway** — `GET /request` → data-service; `timeout` fault → 504 + `upstream_timeouts_total`.
  - **data-service** — `GET /process` → downstream-dep; `bad_config` fault → 500 + `config_errors_total` (+ `config_version` gauge).
  - **downstream-dep** — `GET /data` (leaf); `latency` fault (sleep + warn log) and `memory` fault (holds a byte block, `downstream_memory_bytes` gauge). Multi-fault state (both can be active at once).
- Built the fault-injector CLI (`injector/inject.py`): `--fault/--target/--params/--duration/--clear`, port map (8001/8002/8003, env-overridable), auto-revert on `--duration`, writes ground truth to `injector/ground_truth.jsonl`.
- `docker compose up` brings up all three services + Prometheus; verified the full request chain returns 200, all three Prometheus targets `UP`, and all four faults move their signals (504s, 500s, latency, memory gauge) with ground-truth records written.
- Created a `.venv` at repo root (gitignored) with the test/runtime deps; **36 tests pass** across the four packages.
- Updated `docs/decisions.md` with the Phase 1 entry.

**Current state:**
- Phase 1 functionally complete. Stack is currently **running** in Docker (started this session). Stop it with `docker compose down` if not needed.
- Nothing committed yet this session — see the suggested commits below; git status is otherwise clean from Phase 0.

**Blockers:** None.

**Known tech debt (from end-to-end verification):**
- api-gateway does not gracefully handle a 5xx from data-service: `call_upstream`'s `raise_for_status()` raises an uncaught `httpx.HTTPStatusError`, so the gateway returns a 500 via Starlette's error middleware. That path bypasses the custom metrics middleware, so the gateway 500 is **not** counted in `http_requests_total` and logs a stack trace instead of a structured error. Recommended fix: catch `httpx.HTTPStatusError` in `/request` → return **502** + structured error log + record the metric. (Small; good first task next session.)

**Suggested commits (per micro-step, not yet made):** health → JSON logging → trace_id middleware → metrics → fault control → `/request`+timeout → packaging (api-gateway); then skeleton → fault+endpoint → packaging (data-service, downstream-dep); then injector CLI. All work is currently uncommitted in the working tree.

## Next Session

**Options / pick-up points:**
1. **(Recommended) Commit Phase 1** in logical chunks, then optionally close the api-gateway 502 tech-debt item above as a quick TDD cycle.
2. **Phase 2 — The Agent (Days 8+):** begin the ReAct reasoning loop + LangGraph orchestration + tool layer that will investigate this environment. The victim stack + injector + Prometheus are the substrate it queries.

**To run the stack:** `docker compose up -d --build api-gateway data-service downstream-dep prometheus` (omit `agent-api` — it has no Dockerfile until Phase 2). Inject faults with `python injector/inject.py --fault <t> --target <svc> [--params '{...}'] [--duration N | --clear]`. Prometheus UI at `localhost:9090`.

**To run tests:** from each of `services/*` and `injector/`, run `../../.venv/Scripts/python -m pytest` (or `e:/sre-agent/.venv/Scripts/python`).

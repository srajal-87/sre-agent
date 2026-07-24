# Decisions Log

A running record of decisions, surprises, and trade-offs encountered during the build. Updated daily (3–5 sentences per session). This is the raw material for interview stories and the ARCHITECTURE.md "Decisions & Trade-offs" section.

---

## Phase 0 — Foundation

### Day 1: Repo setup and architecture scaffolding

Set up the repo structure to reflect the multi-service architecture — separate directories for victim services, agent logic, injector, eval, and infra. Chose to keep all victim services in Python/FastAPI for stack homogeneity (polyglot would add complexity without resume signal for this project). Docker Compose is the root orchestration for local dev — each service gets its own Dockerfile, composed together with Prometheus.

Key early decision: the fault injector controls faults via admin endpoints on the victim services (e.g., `POST /admin/fault`) rather than via environment variables or Docker restarts — this keeps injection fast and scriptable without restarting containers. Ground truth is logged by the injector, not the services themselves, so the agent can't accidentally "cheat" by reading the injection log.

CLAUDE.md written with full project context, conventions, and phase tracking — this is the continuity mechanism across Claude Code sessions over the 30-day build.

---

## Phase 1 — Victim System + Fault Injector + Telemetry

### Days 3–7: Three victim services, injector, and telemetry

Built the three FastAPI victim services (api-gateway → data-service → downstream-dep) via strict TDD, then the fault-injector CLI, and brought the whole stack up under docker-compose with Prometheus scraping all three. Each service is self-contained (its own copy of the `logging.py`/`metrics.py`/`faults.py` skeleton) — the minor duplication buys simple Docker build contexts and independent deployability, which we judged worth it for three services. After building api-gateway with full micro-step TDD (7 cycles), we switched to a batched cadence for the other two services (skeleton in one test-backed cycle, then fault+endpoint, then packaging) since the skeleton was now reviewed boilerplate — kept TDD discipline without seven near-identical approval gates each.

Two design choices worth recording. (1) Fault behavior is modeled by observable outcome, not literal mechanism: the gateway `timeout` fault sleeps out its real httpx budget then raises `TimeoutException` (so `http_request_duration_seconds` genuinely spikes), while `bad_config` short-circuits to a deterministic 500 (no latency signal to reproduce). (2) `downstream-dep` needed a multi-fault `FaultState` (a `dict` of active faults) because it supports `latency` and `memory` independently, unlike the single-slot state in the other two services. The `_StdoutHandler` that resolves `sys.stdout` dynamically was an unplanned but necessary fix — a `StreamHandler` caches the stream at construction, which broke pytest's `capsys` capture across tests.

Surprise during end-to-end verification: injecting `latency` (2000 ms) on downstream-dep produced a **504 at the gateway**, not a slow 200 — the added latency exceeded the gateway's real 2 s upstream timeout budget, so the slowness cascaded into a timeout. This is realistic and desirable (it exercises the real timeout path, not just the injected one), and it's exactly the kind of multi-hop symptom the agent will later have to disentangle.

Known gap found in verification (logged as Phase 1 tech debt): api-gateway does **not** gracefully handle a 5xx from data-service — `call_upstream`'s `raise_for_status()` raises `httpx.HTTPStatusError`, which is uncaught, so the gateway returns a 500 via Starlette's error middleware. Because that path bypasses our custom metrics middleware, the gateway 500 is not recorded in `http_requests_total` and logs a stack trace instead of a structured error. Fix deferred to a small follow-up (catch upstream errors → 502 + structured log + metric).

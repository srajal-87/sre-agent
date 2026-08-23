# HANDOFF.md — Session State

This file carries context between Claude Code sessions. Update it at the end of every session. Read it (alongside CLAUDE.md) at the start of every session.

---

## Last Session

**Date:** Day 9 (Phase 3 — sub-phase 3.1: read-only investigation tools)

**What was done:**

Built the three read-only tools the ReAct loop will call, via TDD, one step at a time. The governing goal was that **each tool is individually exercisable against the live stack during a real injected fault before the agent ever calls one** — and that was done, for all four fault types.

- **`agent/` package scaffold** — `config.py` (`PROMETHEUS_URL`, `COMPOSE_PROJECT`, `LOG_SERVICES`, reused `DATABASE_URL`), own `pytest.ini` + `requirements*.txt`, pinned to the same versions as `api/`.
- **Packaging rewired** — `agent-api` now builds from a **root context** (`build: {context: ., dockerfile: api/Dockerfile}`) so the image carries both `api/` and `agent/`. A new root `.dockerignore` is mandatory, not optional. `PROMETHEUS_URL`, `COMPOSE_PROJECT` and the `/var/run/docker.sock` mount added to the service.
- **`agent/tools/base.py`** — `utc_now`, `TimeWindow` (UTC-normalising, `unix_start`/`unix_end` as ints), `ToolResult` envelope, `failure()`.
- **`injector/traffic.py`** — sequential, keep-alive load generator. Metrics don't move without traffic.
- **`query_metrics`** — `KNOWN_METRICS` catalogue + PromQL builder + `query_range` parsing (NaN coercion, capping, stats) + live wiring.
- **`query_logs`** — container stdout over the Docker API, resolved by compose label; line parsing, filters, aggregate-first counting.
- **`query_deploy_history`** — `0002_deploys.sql`, `DeployRepository`, per-record `minutes_before_reference`.
- **`injector/seed_deploys.py`** — noise deploys; **`injector/inject.py`** now writes a *correlated* deploy before enabling a fault (p=0.7) and records the outcome in ground truth.
- **`agent/tools/__init__.py`** registry + drift test + **`agent/tools/probe.py`** (`python -m agent.tools.probe`).
- **`agent/tests/test_smoke_live.py`** — opt-in via `SRE_AGENT_LIVE`, invariants only.

**Current state:**

- Tests: **agent 171 passed, 20 skipped** (the live suite skips without `SRE_AGENT_LIVE`); **injector 45**; **api 13 passed, 1 skipped** (14 with `DATABASE_URL`); Phase 1 regression **36** unchanged.
- Live smoke: `SRE_AGENT_LIVE=1 … pytest tests/test_smoke_live.py` → **20 passed**.
- Rehearsal run for all four faults; each produces correct, distinguishable evidence (see below).
- `deploys` table created in Supabase (migration `0002` applied), seeded with noise deploys plus correlated ones from injections.
- The stack is currently **running** in Docker with no faults active. `docker compose down` if not needed.

**Blockers:** None.

**Important environment note:** the `DATABASE_URL` must be the Supavisor **session pooler** form with the async driver:
`postgresql+asyncpg://postgres.<ref>:<pass>@aws-0-<region>.pooler.supabase.com:5432/postgres`.
A `postgresql://…@db.<ref>.supabase.co:5432` DSN fails twice over — no `+asyncpg` (SQLAlchemy reaches for psycopg2, which is not installed) and `db.<ref>` resolves **IPv6-only**, unreachable from Docker Desktop on Windows. `.env` has been corrected.

**Security follow-up (still outstanding from Day 8):**
- Revoke the Supabase PAT `sbp_75b6…7da` at https://supabase.com/dashboard/account/tokens and issue a fresh one; rotate the DB password if you want it clean.
- **New:** `docker-compose.yml` mounts `/var/run/docker.sock` into `agent-api`. This is **root-equivalent host access** — fine for a local demo stack, required by the Phase 6 `restart_service` tool, but it will not work on Fly.io and must be called out in any writeup.

**Rehearsal results (all four faults, live):**

| Fault | `query_metrics` | `query_logs` |
|---|---|---|
| `latency` (downstream-dep) | p99 rose 0.032 → 4.94s | 16x `injected latency` + 15x `upstream timeout` |
| `timeout` (api-gateway) | `upstream_timeouts_total` rate 0 → 0.446 | 33x `upstream timeout calling data-service` |
| `bad_config` (data-service) | `config_errors_total` rate 0 → 2.41 | 159x `configuration error: invalid downstream target` |
| `memory` (downstream-dep) | `downstream_memory_bytes` 0 → 2.5e+08 | nothing — the gauge is its only observable |

**Known tech debt:** see `CLAUDE.md`. Note that the api-gateway 5xx gap is now *captured and tested*: during `bad_config` the gateway logs zero structured ERROR lines and zero 500s, only raw tracebacks, so `unparsed_count` is its only signal (`agent/tests/test_logs_parse.py`). Fixing the gateway will fail those two tests by design — update them and the tech-debt list together.

## Next Session

**Pick up at: sub-phase 3.2 — the ReAct reasoning loop + LangGraph orchestration.**

The tool layer is ready to plug in: `TOOLS[name]["input_model"].model_json_schema()` is already the exact shape the Anthropic tool-use API wants, and `run_tool(name, arguments_dict)` validates raw model arguments and returns a `ToolResult` — bad arguments and unknown tool names come back as `ok=False` observations rather than exceptions, so nothing the model sends can kill the graph. `ToolResult.model_dump(mode="json")` drops straight into the `investigations.evidence` jsonb column; note that `investigations.steps` is an `int` *count*, not a transcript, so any step-by-step trail must nest inside `evidence`.

**Still deferred (explicitly out of scope for 3.1):**
- Raw-PromQL escape hatch, gated by an allow-list.
- Write tools + policy gate (3.3), LangSmith tracing (Phase 4).
- `agent-api` `/metrics` + Alertmanager (existing tech debt).
- The injector's `ground_truth_*` dual-write to **Postgres** — ground truth still lives only in `injector/ground_truth.jsonl`, though it now also records `correlated_deploy`. Needed before Phase 5.

**To run the stack:** `docker compose up -d --build`. Prometheus at `localhost:9090`, agent-api at `localhost:8000`.

**To run tests:** from each of `services/*`, `injector/`, `api/`, and `agent/`: `e:/sre-agent/.venv/Scripts/python -m pytest`.

**To exercise a tool by hand:**
```bash
docker compose up -d
python injector/traffic.py --target api-gateway --rps 5 --duration 300 &
python injector/seed_deploys.py --noise 5
python injector/inject.py --fault latency --target downstream-dep \
       --params '{"delay_ms": 3000}' --duration 120 --deploy

# during the fault window (PROMETHEUS_URL=http://localhost:9090 from the host):
python -m agent.tools.probe --list
python -m agent.tools.probe --tool query_metrics --args '{"metric":"http_request_duration_seconds","service":"downstream-dep","path":"/data","aggregation":"p99"}'
python -m agent.tools.probe --tool query_logs --args '{"levels":["ERROR","WARNING"],"lookback_minutes":5}'
python -m agent.tools.probe --tool query_deploy_history --args '{"lookback_minutes":120}'
```

**To run the live smoke suite:**
```bash
cd agent
SRE_AGENT_LIVE=1 PROMETHEUS_URL=http://localhost:9090 DATABASE_URL=... \
  e:/sre-agent/.venv/Scripts/python -m pytest tests/test_smoke_live.py
```

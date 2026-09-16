# CLAUDE.md — SRE Agent Project Context

## Project Purpose

An AI-powered SRE agent that investigates production incidents by querying logs, metrics, and deploy history using a ReAct-style reasoning loop, then either auto-remediates low-risk issues or escalates with cited evidence. Built as a portfolio project demonstrating industry-level AI agent design with evaluation, observability, and cost tracking.

## Tech Stack

- **Language:** Python 3.12
- **Agent orchestration:** LangGraph
- **LLM:** Claude / GPT-4-class frontier model (via API)
- **API layer:** FastAPI (async)
- **Metrics:** Prometheus (pull-based scraping)
- **Logging:** Structured JSON to stdout
- **Storage:** Supabase (managed Postgres, free tier)
- **Observability:** LangSmith (agent traces) + Postgres (investigation summaries)
- **Evaluation:** Custom deterministic scoring against ground-truth fault labels
- **Deployment:** Docker Compose on Fly.io (free tier), GitHub Actions CI/CD
- **Demo UI:** React (thin, two pages)

## Architecture — Component Status

| # | Component                | Status      |
|---|--------------------------|-------------|
| 1 | Victim System            | Complete    |
| 2 | Fault Injector           | Complete    |
| 3 | Telemetry (Prometheus)   | Complete    |
| 4 | Reasoning Loop (ReAct)   | Built, live rehearsal pending |
| 5 | Orchestration (LangGraph)| Built, live rehearsal pending |
| 6 | Tool/Action Layer        | In progress |
| 7 | Storage (Supabase)       | Complete    |
| 8 | Audit Trail (LangSmith)  | Built, live rehearsal pending |
| 9 | Evaluation Harness       | Not started |
| 10| API Layer (FastAPI)      | In progress |
| 11| Deployment (Fly.io)      | Not started |
| 12| Demo UI (React)          | Not started |

## Current Phase

**Phase 4 — Audit trail**, with one Phase 3 gate still open behind it.

- **3.1** (the three read-only tools) — complete.
- **3.2** (the ReAct loop and LangGraph orchestration, in `agent/graph/`) — built and
  rehearsed live, but **its `timeout` gate is still open**: three live runs blamed the wrong
  service three times. Until that closes, the loop's diagnosis is unproven for that fault.
- **3.3** (write tools + the deterministic policy gate) — built and proven live: the agent
  diagnosed a `bad_config` fault, proposed `toggle_config`, the gate approved it, and
  Prometheus confirmed the fault cleared before the injector would have reverted it.
- **Phase 4** (LangSmith tracing + the Postgres per-investigation summary, `agent/graph/trace.py`
  and `api/app/records.py`) — built and tested offline; **the live gate has not been run**.

**Next:** one traced live run through `POST /investigate` that lands in both LangSmith and
Postgres. See `HANDOFF.md` for the recipe and its traps.

Phase numbering follows `README.md`'s roadmap (Phase 2 = storage + API shell, Phase 3 = agent core).

## Key Conventions

- All victim services are Python (FastAPI), containerized, exposing `/health` and `/metrics` endpoints.
- All services write structured JSON logs to stdout (fields: timestamp, service, level, message, trace_id).
- Agent tools are defined as Pydantic models in `agent/tools/`, registered in
  `agent/tools/__init__.py` (`TOOLS`) and invoked via `run_tool(name, arguments)`.
- **A tool never raises for an expected failure.** Backend down, unknown metric name,
  malformed arguments — all return `ok=False` with a readable `error`, because the
  caller is an LLM and an exception kills the graph. Pure helpers inside a tool still
  raise; only the tool boundary converts. This deliberately diverges from the
  `ValueError`/`RuntimeError` convention used in human-facing code.
- **Empty is not failure.** `ok=True` with zero rows is a first-class answer — "no
  deploys in the window" excludes a code change, and flat traffic *is* the evidence
  under a timeout fault.
- Every tool result carries `source` + `query` + `window` (the literal query issued and
  the range it covered), which is what makes a cited diagnosis mechanically possible.
- Tool-facing strings (summaries, notes, errors) are **ASCII** — they get printed to a
  Windows console by `agent/tools/probe.py`.
- Collaborators (HTTP fetch, Docker client, sessionmaker, clock) are injected as
  keyword arguments with real defaults — no mocking library, no `pytest-asyncio`;
  coroutines are driven with `asyncio.run(...)`.
- **The graph's own control tool (`update_hypothesis`) never goes in `TOOLS`.** That registry
  means "the read-only investigation surface" — it is what `probe.py` offers and what the
  drift test guards. The graph owns its control tool in `agent/graph/hypothesis.py` and
  merges it into the tool list it sends the model.
- **Every requested tool call produces exactly one evidence entry.** The model's assistant
  turn is replayed verbatim and the API rejects a `tool_use` with no matching `tool_result`,
  so a call the executor declines (duplicate, over the per-turn cap) gets a synthetic result
  saying why. Declining is not dropping.
- **The confidence in a report is validated, not reported.** `decide` caps it deterministically
  when a citation does not resolve to a `query` some tool actually issued, or when there are
  no citations at all. The graph never treats the model's number as final.
- **Nothing per-run goes in the system prompt** (`agent/prompts/system.py`) — no timestamp, no
  alert detail, no host URL. Caching is a prefix match over `tools` → `system` → `messages`, so
  one varying byte there invalidates the cache on every call. Per-run values go in the first
  user message.
- **Observability is fail-open.** Every function in `agent/graph/trace.py` degrades to "not
  traced" rather than raising — a missing key, a failed import or a dead network must never
  cost an investigation. It is the tool layer's never-raises rule applied to the audit trail.
  Tracing is also *attached*, not ambient: the tracer is passed as a callback and the project
  comes from `agent/config.py`, so a run is traced because the code asked for it.
- **The trace id is minted before the run, not read back after it.** `run_id` is a valid
  `RunnableConfig` key and becomes the root run's id, so the id is knowable even for a run that
  fails — but it only reaches the report when the run was actually traced, because a report
  must never cite a trace that does not exist.
- The policy table in `agent/policy/` is always deterministic — never LLM-evaluated.
- Ground truth for every injected fault is logged by the injector to a known location and to Postgres.
- The fault injector controls faults via admin endpoints on victim services (e.g., `POST /admin/fault`).

## Project Layout

```
sre-agent/
├── services/                  # Victim system (monitored services)
│   ├── api-gateway/           # Service 1: routing, timeout faults
│   ├── data-service/          # Service 2: config-based faults
│   └── downstream-dep/        # Service 3: latency/memory faults
├── agent/                     # The SRE agent
│   ├── config.py              # PROMETHEUS_URL, COMPOSE_PROJECT, LOG_SERVICES
│   ├── graph/                 # LangGraph nodes, edges, state schema
│   ├── tools/                 # base.py, metrics.py, logs.py, deploys.py, probe.py
│   ├── policy/                # Blast-radius/confidence policy table
│   ├── prompts/               # System prompts, investigation templates
│   └── tests/                 # Unit + opt-in live smoke; fixtures/ is captured output
├── injector/                  # Fault injection CLI, traffic generator, deploy seeder
├── eval/                      # Evaluation harness + scenario definitions
│   └── scenarios/             # YAML/JSON scenario files
├── api/                       # FastAPI service wrapping the agent (agent-api)
├── supabase/migrations/       # Checked-in .sql migrations (no Alembic)
├── ui/                        # React demo app
├── infra/                     # Prometheus config, deployment configs
├── docs/                      # Architecture docs, decisions log
├── tests/                     # Integration + unit tests
├── .github/workflows/         # GitHub Actions CI/CD
└── docker-compose.yml         # Root compose for local dev
```

## How to Run (once built)

```bash
# Start all services locally
docker-compose up --build

# Generate traffic (metrics only move when requests flow)
python injector/traffic.py --target api-gateway --rps 5 --duration 300

# Seed the deploy ledger with uncorrelated noise
python injector/seed_deploys.py --noise 5

# Inject a fault (example). --deploy / --no-deploy forces whether a correlated
# deploy is written first; omitting it decides at random.
python injector/inject.py --fault timeout --target api-gateway --duration 60

# Run one tool by hand and print what the agent would see
python -m agent.tools.probe --list
python -m agent.tools.probe --tool query_logs --args '{"levels":["ERROR"],"lookback_minutes":5}'

# Run one investigation by hand and print what the agent concluded
# (exit codes: 0 = completed, 1 = the investigation failed, 2 = bad usage)
python -m agent.graph.run --alert eval/scenarios/gateway-timeout.json
python -m agent.graph.run --alert eval/scenarios/downstream-memory.json --json

# Record an incident AND investigate it (the run happens in a background task;
# the response is 201 pending, and the row fills in when the run finishes).
# Set AGENT_AUTO_INVESTIGATE=false to get the old record-only stub behaviour.
curl -X POST http://localhost:8000/investigate -H "Content-Type: application/json" -d '{"alerts": [...]}'
curl http://localhost:8000/investigations/<investigation_id>

# Run evaluation suite
python eval/run_eval.py
```

## Known Issues / Tech Debt

- **api-gateway does not handle a 5xx from data-service.** `call_upstream`'s `raise_for_status()`
  raises an uncaught `httpx.HTTPStatusError`, so the gateway returns a 500 via Starlette's error
  middleware — bypassing the metrics middleware (so it is never counted in `http_requests_total`)
  and logging a stack trace instead of a structured error. Fix: catch it in `/request` → 502 +
  structured log + metric. **This is now captured and tested**: during a `bad_config` fault the
  gateway emits zero structured ERROR lines and zero 500s, only raw tracebacks, so `query_logs`'
  `unparsed_count` is its only signal (`agent/tests/test_logs_parse.py`). Fixing the gateway will
  fail those two tests by design — update them and this entry together.
- **`POST /investigate` has never run the real graph.** The wiring is built and offline-tested
  against a fake investigator, but every test that exercises the endpoint substitutes one —
  `api/tests/conftest.py` turns `AGENT_AUTO_INVESTIGATE` off for the whole suite precisely so a
  forgotten override cannot make a paid model call. The live rehearsal is outstanding.
- **A retried webhook buys a second paid investigation.** There is no dedupe on
  `POST /investigate`: two deliveries of the same alert open two incidents and run the agent
  twice. Harmless today because there is no Alertmanager in the stack, and a blocker the moment
  there is.
- **An investigation in flight at shutdown is lost.** `BackgroundTasks` keeps it inside the
  app's lifetime, but `dispose_engine()` closes the pool at shutdown and the row stays
  `running` with nothing to finish it. A task registry is the fix if it ever matters.
- **LangSmith cannot price Bedrock model ids**, so its UI shows `$0` for every run.
  `estimate_cost` in `agent/graph/llm.py` is the only cost number that reaches Postgres, and
  the one to trust.
- **The opening sweep's metric and log calls are anchored to *now*, not to `reference_time`.**
  Only `query_deploy_history` uses the frozen incident time. That is right during a live fault
  (anchoring to the alert's start would cut off the most recent minutes, where an ongoing fault
  is most visible) but it means replaying an hours-old incident reads the wrong window. The fix
  is passing `since`/`until` on those calls.
- **`agent-api` exposes no `/metrics`** and has no Prometheus scrape target in `infra/prometheus.yml`.
- **No Alertmanager.** `infra/prometheus.yml` has no alerting rules and no Alertmanager container,
  so `POST /investigate` is exercised with fixture payloads rather than live alerts.
- **Injector does not dual-write ground truth to Postgres.** The `ground_truth_*` columns on
  `incidents` exist but stay null; ground truth lives only in `injector/ground_truth.jsonl`
  (which does now also record `correlated_deploy`). Needed before Phase 5.
- **`agent-api` mounts `/var/run/docker.sock`**, which `query_logs` needs to read container
  stdout. This is root-equivalent host access: acceptable for a local demo stack, and the
  Phase 6 `restart_service` tool needs it too, but it will not work on Fly.io.
- **Prometheus' TSDB is ephemeral** — no volume is mounted at `/prometheus`, so all history
  dies on `docker compose down`. An empty `query_metrics` result after a restart means "no
  retained samples", not "no traffic".

---

## Ways of Working (MANDATORY for all implementation tasks)

These rules govern every session where production code is written or modified. They override Claude's default behavior.

### Core Rules

- Work in **very small steps** — one logical change at a time. Never bundle multiple concerns.
- **Do not generate a full solution in one go.** Present one step, wait, repeat.
- After every step: stop, explain what was done, suggest a commit message, and ask for confirmation before proceeding.
- Prefer **simple, readable, maintainable** code over clever code. Optimise for the next reader.
- If any part of the spec is unclear, **ask instead of assuming** — never guess intent.

### Output Format (one step at a time, strictly)

Present each step using this exact structure:

```
### Step N — <short description>

**What this step does:**
<one sentence>

**Test (failing):**
<file path>
<test code — must fail before implementation exists>

Waiting for your approval. Reply ✓ to implement, or describe changes.
```

After approval, continue with:

```
**Implementation (minimal):**
<file path>
<only the code needed to make the test pass — nothing more>

**Refactor (if applicable):**
<only if there is a clear improvement; skip if not needed>

**Does it pass?** `pnpm --filter <package> run test`

**Suggested commit:** `<type>(<scope>): <short description>`

Ready for Step N+1 — [brief description of next step]. Proceed?
```

Commit type order per cycle: `test:` → `feat:` → `refactor:`

### When Planning

- Present multiple options with pros/cons when they exist.
- Call out edge cases and how we should handle them.
- Ask clarifying questions rather than making assumptions.
- Question design decisions that seem suboptimal.
- Share opinions on best practices, but acknowledge when something is opinion vs fact.

---

## Session Workflow

- **CLAUDE.md is stable project context.** Do not edit it per-session. It changes only when the project's architecture, conventions, or tech stack change.
- **HANDOFF.md carries session-to-session state.** At the end of every session, update `HANDOFF.md` with: what was done, current state/blockers, and what the next session should pick up. At the start of every session, read this file first alongside CLAUDE.md.
- **docs/decisions.md** is the running decisions log (3–5 sentences per session, capturing decisions, trade-offs, and surprises). This is separate from HANDOFF.md — decisions.md is permanent project history; HANDOFF.md is ephemeral session state.
- Prefer starting a **fresh Claude Code session for each new phase**. Within a phase, continue the same session when possible.
- If a session gets confused or stuck, start fresh — update HANDOFF.md first so context isn't lost.

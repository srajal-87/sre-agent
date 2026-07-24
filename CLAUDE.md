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
| 4 | Reasoning Loop (ReAct)   | Not started |
| 5 | Orchestration (LangGraph)| Not started |
| 6 | Tool/Action Layer        | Not started |
| 7 | Storage (Supabase)       | Not started |
| 8 | Audit Trail (LangSmith)  | Not started |
| 9 | Evaluation Harness       | Not started |
| 10| API Layer (FastAPI)      | Not started |
| 11| Deployment (Fly.io)      | Not started |
| 12| Demo UI (React)          | Not started |

## Current Phase

**Phase 1 — Victim System + Fault Injector + Telemetry (Days 3–7):** Complete
**Next:** Phase 2 — Reasoning Loop (ReAct) + Orchestration (LangGraph) + Tool/Action Layer

## Key Conventions

- All victim services are Python (FastAPI), containerized, exposing `/health` and `/metrics` endpoints.
- All services write structured JSON logs to stdout (fields: timestamp, service, level, message, trace_id).
- Agent tools are defined as Pydantic models in `agent/tools/`.
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
│   ├── graph/                 # LangGraph nodes, edges, state schema
│   ├── tools/                 # Tool definitions (Pydantic models)
│   ├── policy/                # Blast-radius/confidence policy table
│   └── prompts/               # System prompts, investigation templates
├── injector/                  # Fault injection CLI + ground-truth logger
├── eval/                      # Evaluation harness + scenario definitions
│   └── scenarios/             # YAML/JSON scenario files
├── api/                       # FastAPI service wrapping the agent
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

# Inject a fault (example)
python injector/inject.py --fault timeout --target api-gateway --duration 60

# Trigger an investigation
curl -X POST http://localhost:8000/investigate -H "Content-Type: application/json" -d '{"alerts": [...]}'

# Run evaluation suite
python eval/run_eval.py
```

## Known Issues / Tech Debt

- None yet (Phase 0)

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

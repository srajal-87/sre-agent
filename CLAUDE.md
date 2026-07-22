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
| 1 | Victim System            | Not started |
| 2 | Fault Injector           | Not started |
| 3 | Telemetry (Prometheus)   | Not started |
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

**Phase 0 — Foundation (Days 1–2)**
- Status: Complete
- Repo structure, docs, docker-compose skeleton created
- Next: Phase 1 — Victim System + Fault Injector + Telemetry

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

## Session Notes

- When starting a new session, read this file first, then check `docs/decisions.md` for recent context.
- Prefer starting a fresh Claude Code session for each new phase.
- Within a phase, continue the same session when possible.

# SRE Incident Triage Agent

An AI-powered agent that automates the initial triage phase of incident response. When an alert fires, the agent investigates by querying metrics, logs, and deploy history — the same signals a human on-call engineer would check — and produces a diagnosis with cited evidence. For low-risk, high-confidence scenarios, it auto-remediates. For everything else, it escalates with a full investigation trail.

## Why This Exists

In a real on-call rotation, the first 10–15 minutes of every incident are spent on orientation: reading the alert, checking what deployed recently, scanning dashboards, forming a hypothesis. This is repetitive, cognitively expensive, and often happens at 3am. This agent handles that triage phase — pulling from the same signal sources a human would — so the on-call engineer starts with a diagnosis and evidence, not a raw alert.

## Architecture

See [ARCHITECTURE.md](./ARCHITECTURE.md) for the full system design, component breakdown, and trade-off decisions.

## Quick Start

```bash
# Prerequisites: Docker, Docker Compose

# Clone and start all services
git clone <repo-url>
cd sre-agent
cp .env.example .env  # Add your API keys
docker-compose up --build

# Inject a fault and trigger an investigation
python injector/inject.py --fault timeout --target api-gateway
curl -X POST http://localhost:8000/investigate \
  -H "Content-Type: application/json" \
  -d @eval/scenarios/sample-alert.json

# Run the evaluation suite
python eval/run_eval.py
```

## Evaluation Results

<!-- Will be populated after Phase 5 -->

| Metric | Value |
|--------|-------|
| Diagnosis accuracy | TBD |
| False autonomous action rate | TBD |
| Avg cost per investigation | TBD |
| Avg latency per investigation | TBD |

## Key Design Decisions

<!-- Will be populated from docs/decisions.md on Day 30 -->

## Tech Stack

| Component | Choice | Why |
|-----------|--------|-----|
| Agent orchestration | LangGraph | Explicit state machine for the investigation loop's branch logic (act vs. escalate) |
| LLM | Claude / GPT-4 class | Reliable multi-step tool selection at frontier quality |
| API | FastAPI | Async I/O for LLM-bound calls, Pydantic schema validation |
| Metrics | Prometheus | Industry standard, agent queries via HTTP API |
| Storage | Supabase (Postgres) | Managed, free tier, typed investigation records |
| Observability | LangSmith | Native LangGraph tracing with near-zero instrumentation |
| Deployment | Docker Compose on Fly.io | Full stack on one host, GitHub Actions CI/CD |

## Project Status

- [x] Phase 0: Foundation & repo setup
- [ ] Phase 1: Victim system + fault injector + telemetry
- [ ] Phase 2: Storage + API shell
- [ ] Phase 3: Agent core (reasoning loop, tools, policy)
- [ ] Phase 4: Observability & audit trail
- [ ] Phase 5: Evaluation harness
- [ ] Phase 6: Deployment & CI/CD
- [ ] Phase 7: Demo UI
- [ ] Day 30: Polish & demo prep

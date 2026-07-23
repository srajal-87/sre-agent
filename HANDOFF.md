# HANDOFF.md — Session State

This file carries context between Claude Code sessions. Update it at the end of every session. Read it (alongside CLAUDE.md) at the start of every session.

---

## Last Session

**Date:** Day 1–2 (Phase 0)
**What was done:**
- Created full repo scaffold: services/, agent/, injector/, eval/, api/, ui/, infra/, docs/, tests/
- Wrote CLAUDE.md (project context + Ways of Working + session workflow)
- Wrote ARCHITECTURE.md (component descriptions + Mermaid data flow diagram)
- Started docs/decisions.md with first entry
- Created docker-compose.yml skeleton (all services defined, not yet buildable)
- Created infra/prometheus.yml scrape config
- Created .env.example, .gitignore, README.md

**Current state:**
- Phase 0 is complete. No running code yet — all files are scaffolding/config.
- docker-compose.yml references Dockerfiles that don't exist yet (expected — Phase 1 creates them).

**Blockers:** None.

## Next Session

**Phase 1 — The Environment (Days 3–7)**
- Build the three victim services (api-gateway, data-service, downstream-dep), each with:
  - A Dockerfile
  - A FastAPI app with `/health`, `/metrics`, and `/admin/fault` endpoints
  - One injectable fault type per service
  - Structured JSON logging to stdout
- Build the fault injector CLI script
- Get `docker-compose up` running all three services + Prometheus
- Verify Prometheus scrapes all targets and faults are observable

**Start by:** Reading CLAUDE.md, then asking Claude Code to plan the api-gateway service before writing code.

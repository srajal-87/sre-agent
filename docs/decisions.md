# Decisions Log

A running record of decisions, surprises, and trade-offs encountered during the build. Updated daily (3–5 sentences per session). This is the raw material for interview stories and the ARCHITECTURE.md "Decisions & Trade-offs" section.

---

## Phase 0 — Foundation

### Day 1: Repo setup and architecture scaffolding

Set up the repo structure to reflect the multi-service architecture — separate directories for victim services, agent logic, injector, eval, and infra. Chose to keep all victim services in Python/FastAPI for stack homogeneity (polyglot would add complexity without resume signal for this project). Docker Compose is the root orchestration for local dev — each service gets its own Dockerfile, composed together with Prometheus.

Key early decision: the fault injector controls faults via admin endpoints on the victim services (e.g., `POST /admin/fault`) rather than via environment variables or Docker restarts — this keeps injection fast and scriptable without restarting containers. Ground truth is logged by the injector, not the services themselves, so the agent can't accidentally "cheat" by reading the injection log.

CLAUDE.md written with full project context, conventions, and phase tracking — this is the continuity mechanism across Claude Code sessions over the 30-day build.

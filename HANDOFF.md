# HANDOFF.md — Session State

This file carries context between Claude Code sessions. Update it at the end of every session. Read it (alongside CLAUDE.md) at the start of every session.

---

## Last Session

**Date:** Day 8 (Phase 2 — Storage + API shell)

**What was done:**
- **Supabase project created** (personal org, not Zenisth AI): ref `usfekcxmnqkhtembnlqk`, region `ap-south-1`, Postgres 17.6, `ACTIVE_HEALTHY`. Created via the **Management API**, not MCP — the in-session MCP server only sees the Zenisth AI org.
- **Schema applied** from `supabase/migrations/0001_incidents_investigations.sql`: `incidents` (14 cols, incl. nullable `ground_truth_*`) and `investigations` (15 cols), FK `on delete cascade`, CHECK constraints on both `status` columns, `created_at desc` + `group_key` / `service` / `incident_id` indexes, RLS enabled with **no policies**.
- **`api/` service (`agent-api`) built via TDD**, mirroring the victim-service layout (own `Dockerfile`, `requirements.txt`, `pytest.ini`, verbatim copy of `app/logging.py`):
  - `GET /health`
  - `POST /investigate` — Alertmanager webhook → incident row + `status='pending'` investigation stub in one transaction → `201 {incident_id, investigation_id, status}`
  - `GET /investigations/{id}` — the row; unknown id → `404 {"error": "investigation not found"}`; non-UUID → `422`
  - `app/{config,models,db,schemas,repository}.py` — `os.getenv` config, SQLAlchemy 2.x async ORM mirroring the migration, lazily-built engine + sessionmaker (so imports work without a `DATABASE_URL`), Pydantic v2 camelCase webhook schemas, thin `IncidentRepository`.
- **Verified end-to-end against real Supabase**: integration test passes, and a manual `uvicorn` run produced a real incident + joined `pending` investigation, confirmed via a Management API `select`.
- Docs updated: `.env.example` (pooler DSN + the IPv6 warning), `README.md` (Phases 1–2 checked), `CLAUDE.md` (component table, current phase, tech-debt list, `supabase/` in the layout), `docs/decisions.md` (Phase 2 entry).

**Current state:**
- Phase 2 complete and **committed** (Phase 1 was also committed at the start of this run; `main` is clean).
- Tests: **api 13 passed, 1 skipped** (the integration test skips without `DATABASE_URL`); Phase 1 regression **36 passed** (15 + 8 + 9 + 4).
- `.env` exists at the repo root (gitignored) with the working `DATABASE_URL`.

**Blockers:**
- **Docker Desktop was not running**, so `docker compose up -d --build agent-api` was never executed. The image build is therefore *unverified* — everything was checked via host `uvicorn` instead. **First task next session: start Docker Desktop and run it.**

**Security follow-up (outstanding):**
- The Supabase PAT `sbp_75b6…7da` has been pasted into two session transcripts. **Revoke it** at https://supabase.com/dashboard/account/tokens and issue a fresh one. The DB password is likewise in the transcript and in `.env` — rotate it in the dashboard if you want it clean, and update `.env`.

**Known tech debt:** see the list in `CLAUDE.md` (api-gateway 502 handling, no `/metrics` on `agent-api`, no Alertmanager, injector doesn't dual-write ground truth to Postgres).

## Next Session

**Options / pick-up points:**
1. **(Do first, 5 min)** Start Docker Desktop → `docker compose up -d --build` → confirm all five services come up and `curl localhost:8000/health` works from the container.
2. **(Recommended) Phase 3 — Agent core:** the ReAct reasoning loop + LangGraph orchestration + tool layer. `investigations` rows currently stay `pending` forever; Phase 3's job is to fill them in (`diagnosis`, `confidence`, `evidence`, `steps`, `cost_usd`, `latency_ms`).
3. Optional small cycles: the api-gateway 502 fix, `/metrics` on `agent-api` + its Prometheus scrape target, or the injector → Postgres ground-truth dual-write (which makes the eval harness possible later).

**To run the stack:** `docker compose up -d --build` (all five services; `agent-api` now has a Dockerfile and reads `DATABASE_URL` from `.env`). Inject faults with `python injector/inject.py --fault <t> --target <svc> [--params '{...}'] [--duration N | --clear]`. Prometheus UI at `localhost:9090`, agent-api at `localhost:8000`.

**To run tests:** from each of `services/*`, `injector/`, and `api/`, run `e:/sre-agent/.venv/Scripts/python -m pytest`. To include the DB integration test: `DATABASE_URL=... e:/sre-agent/.venv/Scripts/python -m pytest` from `api/`.

**Manual API check:**
```bash
curl -X POST http://localhost:8000/investigate -H "Content-Type: application/json" \
     -d @api/tests/fixtures/alertmanager_firing.json
curl http://localhost:8000/investigations/<investigation_id>
```

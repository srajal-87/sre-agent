-- 0002_deploys.sql
--
-- A synthetic deploy ledger, read by the agent's query_deploy_history tool.
--
-- There is no real deploy history in this project: no git tags, no CI release
-- workflow, no build_info metric, no image tags. This table is that history,
-- written by injector/seed_deploys.py (background noise) and by
-- injector/inject.py (the deploy correlated with an injected fault).
--
-- Postgres rather than a JSONL file because the Phase 5 eval harness has to
-- correlate deploys with incidents.ground_truth_*, which live here; a file
-- would force a cross-source join.
--
-- RLS is enabled with NO policies, matching 0001: the API connects as the
-- `postgres` role (which bypasses RLS), so this denies all PostgREST/anon-key
-- access while keeping Supabase's security advisor clean.

create table if not exists deploys (
    id               uuid primary key default gen_random_uuid(),
    service          text not null,
    version          text not null,                  -- 'v2.3.0'
    commit_sha       text not null,
    author           text not null,
    message          text not null,                  -- commit subject
    changed_files    jsonb not null default '[]'::jsonb,
    deployed_at      timestamptz not null,
    status           text not null default 'succeeded'
                     check (status in ('succeeded', 'failed', 'rolled_back')),
    -- Forward-compat for the Phase 6 rollback_deploy write tool.
    rolled_back_from uuid references deploys (id)
);

create index if not exists deploys_deployed_at_idx on deploys (deployed_at desc);
create index if not exists deploys_service_idx on deploys (service);

alter table deploys enable row level security;

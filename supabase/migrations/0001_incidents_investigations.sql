-- 0001_incidents_investigations.sql
--
-- Core persistence for the SRE agent.
--
--   incidents      — one row per Alertmanager webhook delivery, plus nullable
--                    ground-truth columns the fault injector will dual-write in
--                    a later phase (ARCHITECTURE.md §7).
--   investigations — one row per agent run against an incident. Created as a
--                    'pending' stub by POST /investigate and filled in later.
--
-- RLS is enabled with NO policies on both tables: the API connects as the
-- `postgres` role (which bypasses RLS), so this denies all PostgREST/anon-key
-- access while keeping Supabase's security advisor clean.

create table if not exists incidents (
    id                  uuid primary key default gen_random_uuid(),
    created_at          timestamptz not null default now(),
    source              text not null default 'alertmanager',
    status              text not null check (status in ('firing', 'resolved')),
    receiver            text,
    group_key           text,
    service             text,
    common_labels       jsonb not null default '{}'::jsonb,
    alert_count         int not null,
    raw_payload         jsonb not null,
    -- Ground truth: null until the injector dual-writes it (later phase).
    ground_truth_fault  text,
    ground_truth_target text,
    fault_started_at    timestamptz,
    fault_ended_at      timestamptz
);

create index if not exists incidents_created_at_idx on incidents (created_at desc);
create index if not exists incidents_group_key_idx on incidents (group_key);
create index if not exists incidents_service_idx on incidents (service);

create table if not exists investigations (
    id                 uuid primary key default gen_random_uuid(),
    incident_id        uuid not null references incidents (id) on delete cascade,
    created_at         timestamptz not null default now(),
    updated_at         timestamptz not null default now(),
    status             text not null default 'pending'
                       check (status in ('pending', 'running', 'completed', 'failed')),
    diagnosis          text,
    confidence         numeric,
    action_taken       text,
    action_result      text,
    evidence           jsonb,
    steps              int,
    cost_usd           numeric,
    latency_ms         int,
    langsmith_trace_id text,
    error              text
);

create index if not exists investigations_created_at_idx on investigations (created_at desc);
create index if not exists investigations_incident_id_idx on investigations (incident_id);

alter table incidents enable row level security;
alter table investigations enable row level security;

# HANDOFF.md — Session State

This file carries context between Claude Code sessions. Update it at the end of every session. Read it (alongside CLAUDE.md) at the start of every session.

---

## Last Session

**Date:** Day 12 (Phase 3 — sub-phase 3.2: the live rehearsal)

**What was done:**

Ran the 3.2 gate: the graph's first real API calls, against the live stack, one run per
injectable fault. **Result: 3 of 4 passed. The gate is NOT closed** — see the `timeout`
miss below. `CLAUDE.md` was therefore left untouched (components 4 and 5 still read
"Built, live rehearsal pending", Current Phase still 3.2).

First, a prerequisite the rehearsal could not have been honest without: **the deploy ledger
was restored.** It held 7 rows all dated 2026-08-21, and `query_deploy_history` clamps
`lookback_minutes` to 1440, so the agent could not see any of them — it was getting "no
deploys, exclude a code change" for free. Reseeded with
`python injector/seed_deploys.py --noise 6 --hours 2`. **The 2 hours matters:**
`SWEEP_DEPLOYS_MINUTES` is 120 ([agent/graph/nodes.py](agent/graph/nodes.py)), so
`seed_deploys.py`'s default `--hours 6` would have put most noise outside the window the
sweep actually reads. The 7 stale rows were kept (unreachable anyway, and they keep
`ledger_is_empty` honestly false).

Per-run shape, repeated four times: `docker compose restart prometheus` (a clean 30m metric
window — the sweep is anchored to *now*, so back-to-back runs otherwise bleed into each
other, and the TSDB is ephemeral so this is free), 120s of healthy traffic, inject, then
investigate ~100s in (the rate window is 60s, so a fault is crisp well before the 5m mark).

| Fault | Deploy | fault_type | service | Conf | Steps | Verdict |
|---|---|---|---|---|---|---|
| `timeout` (api-gateway) | none | OK | **data-service — WRONG** | 0.75 | 4 | **FAIL** |
| `latency` (downstream-dep) | correlated | OK | OK | 0.85 | 1 | pass (caveat) |
| `bad_config` (data-service) | correlated | OK | OK | 0.85 | 1 | pass |
| `memory` (downstream-dep) | none | OK | OK | 0.75 | 3 | pass |

**Both Day 11 unknowns are resolved.** The `eu.` inference-profile id works — no 404, no
`AGENT_MODEL` override needed. **Bedrock does honour the explicit `cache_control`
breakpoint**: every call after the first in a run read exactly 2511 cached tokens (the tool
block). Single-call runs show `cache_read=0` simply because there is nothing to read back
yet. Cost is at Sonnet rates, not the 1.7x Opus fallthrough — a one-call run billed $0.0483
vs a naive $0.0389, the gap being exactly the 1.25x cache *write* on that 2511-token prefix.
All four runs: **$0.44 total**.

**Citations: 15 across four runs, all resolving, zero dangling.** `decide`'s validation held
against a real model. Reports round-tripped through `json.dumps`; all runs well under
`MAX_ITERATIONS`.

**The deploy noise did its job.** On both no-deploy faults the agent had 6 candidate deploys
in view and blamed none — `memory` explicitly rejected the nearest on timing ("v3.7.1
deployed at 11:13, memory jumped at 11:33 — 19 min gap"). On `bad_config` it picked the
*correlated* row out of the noise and named it in the diagnosis (data-service v2.9.9
"refactor configuration loading", touching `app/config.py`), matching ground truth exactly.

`memory` behaved exactly as designed and is the best evidence the loop earns its keep: the
sweep found nothing, so the model took two more turns, went after `downstream_memory_bytes`,
and read the shape correctly as "jumped to 10MB and plateaued, not growing".

`bad_config` reproduced the Phase 1 tech debt live: data-service emitted 500s at 4.97/s while
api-gateway recorded **zero** 500s and zero structured ERRORs.

**Two open defects (each its own TDD cycle — deliberately NOT fixed in the rehearsal):**

1. **`timeout` names the wrong service.** api-gateway logs `"upstream timeout calling
   data-service"` and the agent took that at face value, naming data-service — despite having
   gathered contrary evidence in the same run (data-service p99 *falling* to 44ms, no
   data-service errors, `config_errors_total` flat). It hedged correctly (0.75, escalate)
   rather than asserting certainty. **Unknown whether this is systematic or a one-off — a
   single run is not enough to tell. Re-run `timeout` 2-3x before designing a fix.**
2. **`latency` mis-dated the correlated deploy.** It found the right row (v3.7.1) but ruled
   it out against a symptom onset of 11:05 that it inferred rather than observed; the deploy
   actually preceded the fault by 67s. Same soft spot as (1): temporal reasoning is weaker
   than symptom reading, and the known `reference_time` anchoring limitation sits underneath
   it.

**Mechanical gotcha worth not rediscovering:** `InvestigationReport` does **not** carry
`cache_read_tokens` (it lives in graph state and each `StepRecord`), so `--json` cannot answer
the cache question. It was answered by wrapping the injected `call` collaborator in a
scratchpad driver — the same seam the tests use. No production code changed.

**Current state:** offline regression unchanged and green — **agent 450 passed / 20 skipped**,
**injector 45**, **api 13 passed / 1 skipped**. Run outputs are in the session scratchpad, not
the repo. `injector/ground_truth.jsonl` has four new rows; the `deploys` table has 6 new noise
rows plus 2 correlated.

---

## Previous Session

**Date:** Day 11 (Phase 3 — sub-phase 3.2: move the model call onto Bedrock)

**What was done:**

Repointed the one model seam, `agent/graph/llm.py`, from the first-party `AsyncAnthropic`
client to `AsyncAnthropicBedrock` (Claude Sonnet 4.5 on Amazon Bedrock, `eu-north-1`, bearer
token via `AWS_BEARER_TOKEN_BEDROCK`). No new dependency — `anthropic==1.0.0` already ships
the Bedrock client, and no boto3, because the region is passed explicitly and bearer auth
returns before the SigV4 path.

Everything else in `llm.py` survived untouched: the single explicit `cache_control`
breakpoint (explicit breakpoints work on Bedrock; top-level auto-caching is what the legacy
integration rejects), cost accounting, the never-raises contract, the per-event-loop client
cache. Two things could not survive — Sonnet 4.5 predates both. `thinking` moved from
`{"type":"adaptive"}` to `{"type":"enabled","budget_tokens":4000}`, and `output_config` is
gone entirely since `effort` is a 400 on this model. `AGENT_EFFORT` was removed from
`agent/config.py` rather than left as a knob that does nothing; `BEDROCK_REGION` and
`AGENT_THINKING_BUDGET` replace it. Three tests in `agent/tests/test_llm.py` asserted the old
contract and were inverted, plus one new guard on the Bedrock pricing key.

Also wired: `.env.example` and `docker-compose.yml` now carry `AWS_BEARER_TOKEN_BEDROCK` +
`AWS_REGION` instead of `ANTHROPIC_API_KEY`.

**Current state:** agent tests **450 passed, 20 skipped**, all offline. Client construction
verified to resolve to `https://bedrock-runtime.eu-north-1.amazonaws.com`. **The live
rehearsal still has not run** — it is now the gate for both the loop and this switch at once.

**Watch for at rehearsal:** the `eu.` inference-profile model id is unverified from here (a
404 means try the bare `anthropic.claude-sonnet-4-5-...` id via `AGENT_MODEL`, not a code
change); `cost_usd` in the Sonnet range, not ~1.7x inflated; and `cache_read_tokens > 0` by
the second iteration.

---

## Earlier Session

**Date:** Day 10 (Phase 3 — sub-phase 3.2: ReAct reasoning loop + LangGraph orchestration)

**What was done:**

Built the consumer of the 3.1 tool layer: a LangGraph state machine that takes an alert,
drives the three read tools in a ReAct loop, and produces a diagnosis with a confidence
score and citations that point at real queries. Built via TDD, one step at a time, from
the plan in `~/.claude/plans/read-claude-md-and-handoff-md-rosy-pony.md`.

```
START -> gather_evidence -> reason -> decide -+-> gather_evidence
          (deterministic)   (the one   (pure) |
                            LLM call)         +-> finalize -> END
```

Twelve steps, each its own test-then-implement cycle:

- **`agent/graph/state.py`** — `InvestigationState` (TypedDict + `operator.add` reducers) with
  Pydantic values: `AlertSummary`, `Hypothesis`, `ToolCall`, `EvidenceEntry`, `StepRecord`,
  `AssistantTurn`, `InvestigationReport`, plus `initial_state()`.
- **`agent/graph/hypothesis.py`** — the synthetic `update_hypothesis` tool, deliberately
  **not** in `TOOLS`.
- **`agent/prompts/system.py`** — the cache-stable system prompt (~1050 tokens).
- **`agent/graph/render.py`** — webhook -> `AlertSummary`, the alert brief, summary-first
  evidence rendering.
- **`agent/graph/messages.py`** — the message list, rebuilt from state each turn.
- **`agent/graph/llm.py`** — `call_model` behind an injectable client, `tool_definitions()`
  with the cache breakpoint, `estimate_cost()`.
- **`agent/graph/nodes.py`** — `opening_sweep`, `gather_evidence`, `reason`, `decide`, `route`,
  `finalize`.
- **`agent/graph/__init__.py`** — `build_graph()`, `investigate()`, `RECURSION_LIMIT`.
- **`agent/graph/run.py`** — the CLI, sibling of `agent/tools/probe.py`.
- **`eval/scenarios/*.json`** — four Alertmanager payloads, one per injectable fault.
- **`agent/config.py`** — `AGENT_MODEL`, `BEDROCK_REGION`, `AGENT_MAX_TOKENS`,
  `AGENT_THINKING_BUDGET`, `MAX_TOOL_CALLS_PER_ITERATION`, and the seven stop-condition
  constants.

**Current state:**

- Tests: **agent 449 passed, 20 skipped** (was 171 + 20; **278 new**), **injector 45**,
  **api 13 passed, 1 skipped**. `services/*` not re-run this session (untouched).
- Every new test runs offline: no API key, no Docker, no Postgres, no mocking library.
  The seams are a `FakeModel` (a callable returning scripted `tool_use` turns) and a fake
  `run_tool`.
- New pins in `agent/requirements.txt`: **`anthropic==1.0.0`**, **`langgraph==1.2.11`**
  (pulling `langchain-core==1.6.0`, `langsmith==0.11.1`). Installed in `.venv`.
  `api/Dockerfile` already installs `agent/requirements.txt`, so `docker compose build`
  picks them up.
- **Nothing is committed.** All of 3.2 is in the working tree. Twelve commits' worth of
  work, one per step; suggested messages are in the session transcript, or squash as
  `feat(agent): add the LangGraph investigation loop`.
- **The live rehearsal has NOT been run.** The graph has never made a real API call or run
  against the live stack. That is the first thing the next session should do.

**Blockers:** None. The one open decision is whether to enable server-side refusal
fallbacks (see below).

**Environment note:** `DATABASE_URL` must be the Supavisor **session pooler**
DSN with the async driver — see the Day 9 entry in git history for why. The model call now
goes to **Amazon Bedrock**: `.env` needs `AWS_BEARER_TOKEN_BEDROCK` and
`AWS_REGION=eu-north-1`. That bearer token is **short-lived** (STS-backed, ~12h) — an
expired one fails on auth in a way that reads like a client bug, so check the clock before
blaming the code.

### Design decisions worth knowing before touching this code

- **`update_hypothesis` is not in `TOOLS`.** The registry means "the read-only investigation
  surface"; it is what `probe.py` offers and what the drift alarm guards. The graph owns its
  own control tool and merges it into the tool list.
- **Every requested call produces exactly one evidence entry.** The model's assistant turn is
  replayed verbatim, and the API rejects a `tool_use` with no matching `tool_result` — so a
  call the executor declines (duplicate, or over the 3-per-turn cap) still gets an entry
  carrying a synthetic result that says why. Dropping one is a 400 that loses the run.
- **Messages are rebuilt from state each turn**, not accumulated, so older evidence can
  collapse to its summary line. The cache breakpoint is on the last tool definition, so the
  cached prefix is tools + system; messages were never cached anyway.
- **Thinking blocks are replayed verbatim on the latest assistant turn only** (the API
  requires it, signature included) and stripped from earlier turns.
- **`decide` caps confidence at `UNCITED_CONFIDENCE_CAP` (0.6)** when a citation does not
  resolve to a query some tool actually issued — *and* when there are no citations at all,
  which the plan did not specify but which is otherwise a cheaper route to a high score than
  citing badly. `confident` additionally requires >=2 distinct tools to have returned
  `ok=True`.
- **The graph does not touch Postgres.** `finalize` returns a pure `InvestigationReport`; the
  caller persists it. This is what keeps the tests backend-free.
- State fields added beyond the plan's schema: `assistant_turns`, `consecutive_llm_errors`
  (`errors` is append-only and cannot distinguish "two in a row"), `notes` (graph-level
  qualifications, kept out of `errors`, which means LLM failures only), `report`.
- **Model config:** `eu.anthropic.claude-sonnet-4-5-20250929-v1:0` on Bedrock
  (`AsyncAnthropicBedrock`, bearer-token auth, region passed explicitly so boto3 stays off
  the path), `thinking={"type":"enabled","budget_tokens":4000}`. **No `output_config`** —
  `effort` is a 400 on Sonnet 4.5, and adaptive thinking is 4.6+ only. Thinking is never
  disabled: with it off the model can write a tool call into visible text where it silently
  never runs. The Bedrock model ids are literal keys in `PRICING` ($3/$15) — without them
  `estimate_cost` falls through to the Opus default and overstates spend ~1.7x.
- **Server-side refusal fallbacks were deliberately NOT enabled.** The Anthropic guidance is
  to include them by default on Opus 5, but they require moving the core loop to
  `client.beta.messages` with a beta header, and `decide` already routes a refusal to a real
  report. Revisit if a refusal is ever seen live.
- **Known limitation:** the sweep's metric and log calls use `lookback_minutes` (anchored to
  now inside the tool); only the deploy call is anchored to `reference_time`. Right for a live
  fault, wrong for replaying a hours-old incident. Fix is `since`/`until` on those calls.

## Next Session

**Pick up at: closing the `timeout` miss.** The rehearsal ran (Day 12) and 3 of 4 faults
passed; `timeout` names the wrong service. Until that is resolved the 3.2 gate is open and
`CLAUDE.md` stays as-is.

**1. Characterise the `timeout` miss before designing anything.** Re-run it 2-3 times and see
whether it lands on `data-service` every time or only sometimes — that determines whether the
fix is a prompt change, a `decide` rule, or nothing at all. The recipe (one fault, ~8 min):

```bash
set -a; . ./.env; set +a            # nothing in this repo loads .env
docker compose restart prometheus   # clean 30m window; the sweep anchors to *now*
python injector/traffic.py --target api-gateway --rps 5 --duration 420 &
sleep 120                           # healthy baseline, so "this changed" is visible
python injector/inject.py --fault timeout --target api-gateway --duration 300 --no-deploy &
sleep 100                           # 60s rate window now fully inside the fault
PROMETHEUS_URL=http://localhost:9090 python -m agent.graph.run \
    --alert eval/scenarios/gateway-timeout.json --json > out.json
```

Then check against `tail -1 injector/ground_truth.jsonl`. The substantive question: the agent
had evidence that data-service was *healthy* (p99 falling, no errors) and named it anyway,
on the strength of api-gateway's `"upstream timeout calling data-service"` log string. Any
fix should make the loop weigh gathered evidence against a log message's implied blame —
not just harden the prompt against this one sentence.

Note the deploy ledger decays: `seed_deploys.py --noise N --hours 2` must be re-run if more
than ~2h have passed, or `query_deploy_history` sees an empty window again
(`SWEEP_DEPLOYS_MINUTES` is 120).

**2. The `SRE_AGENT_LIVE` graph smoke test** — one gated run in
`agent/tests/test_smoke_live.py` asserting invariants only (terminates, produces a report,
citations resolve), never a specific diagnosis. Written but for the live suite; **not done**.

**3. Then sub-phase 3.3** — write tools + the deterministic policy gate.

**Also worth folding in when convenient:** the `latency` run mis-dated the correlated deploy
(inferred a symptom onset rather than observing one). The `since`/`until` fix for the sweep's
now-anchored metric and log calls — already on the known-limitations list — is the most
likely lever on both that and the temporal half of the `timeout` miss.

**Still deferred (explicitly out of scope for 3.2):**
- Wiring the graph into `POST /investigate` as a background task, and persisting the report.
- LangSmith tracing (Phase 4).
- Raw-PromQL escape hatch, gated by an allow-list.
- The injector's `ground_truth_*` dual-write to Postgres — still only in
  `injector/ground_truth.jsonl`. Needed before Phase 5.
- `agent-api` `/metrics` + Alertmanager (existing tech debt).

**Note:** `CLAUDE.md` was deliberately **not** updated on Day 12. Components 4 and 5 still
read "Built, live rehearsal pending" and Current Phase still points at 3.2, because the
rehearsal did not fully pass. Flip both to Complete and move Current Phase to 3.3 once
`timeout` matches ground truth — that file changes only when the architecture genuinely
does, and a gate that is still open is not that.

**To run tests:** from each of `services/*`, `injector/`, `api/`, and `agent/`:
`e:/sre-agent/.venv/Scripts/python -m pytest`.

**To exercise the graph by hand:**
```bash
python -m agent.graph.run --alert eval/scenarios/gateway-timeout.json
python -m agent.graph.run --alert eval/scenarios/downstream-memory.json --json
python -m agent.graph.run --alert <file> --reference-time 2026-08-25T12:00:00Z
```
Exit codes: 0 = completed, 1 = the investigation failed, 2 = bad usage.

**To exercise one tool by hand:** unchanged from 3.1 —
`python -m agent.tools.probe --list`, then `--tool <name> --args '<json>'`.

**To run the live smoke suite:**
```bash
cd agent
SRE_AGENT_LIVE=1 PROMETHEUS_URL=http://localhost:9090 DATABASE_URL=... \
  e:/sre-agent/.venv/Scripts/python -m pytest tests/test_smoke_live.py
```

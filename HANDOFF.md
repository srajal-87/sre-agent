# HANDOFF.md — Session State

This file carries context between Claude Code sessions. Update it at the end of every session. Read it (alongside CLAUDE.md) at the start of every session.

---

## Last Session

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
- **`agent/config.py`** — `AGENT_MODEL`, `AGENT_MAX_TOKENS`, `AGENT_EFFORT`,
  `MAX_TOOL_CALLS_PER_ITERATION`, and the seven stop-condition constants.

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

**Environment note (unchanged):** `DATABASE_URL` must be the Supavisor **session pooler**
DSN with the async driver — see the Day 9 entry in git history for why. `.env` already has
`ANTHROPIC_API_KEY` set.

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
- **Model config:** `claude-opus-5`, `thinking={"type":"adaptive","display":"summarized"}`,
  `output_config={"effort":"high"}`. **No `budget_tokens`** — a 400 on this model. Thinking is
  never disabled: with it off the model can write a tool call into visible text where it
  silently never runs. `display:"summarized"` costs nothing extra and puts the reasoning into
  the audit trail.
- **Server-side refusal fallbacks were deliberately NOT enabled.** The Anthropic guidance is
  to include them by default on Opus 5, but they require moving the core loop to
  `client.beta.messages` with a beta header, and `decide` already routes a refusal to a real
  report. Revisit if a refusal is ever seen live.
- **Known limitation:** the sweep's metric and log calls use `lookback_minutes` (anchored to
  now inside the tool); only the deploy call is anchored to `reference_time`. Right for a live
  fault, wrong for replaying a hours-old incident. Fix is `since`/`until` on those calls.

## Next Session

**Pick up at: the live rehearsal of 3.2.** Everything below is unstarted.

**1. Live end-to-end verification** (the same protocol that closed out 3.1):

```bash
docker compose up -d --build
python injector/traffic.py --target api-gateway --rps 5 --duration 300 &
python injector/seed_deploys.py --noise 5
python injector/inject.py --fault timeout --target api-gateway --duration 120 --deploy

# during the fault window, from the host:
PROMETHEUS_URL=http://localhost:9090 python -m agent.graph.run \
    --alert eval/scenarios/gateway-timeout.json
```

Check, for each of the four faults (`timeout`, `latency`, `bad_config`, `memory`), that:
`fault_type` and `service` match the tail of `injector/ground_truth.jsonl`; confidence >= 0.85
on the clear-cut ones; **every citation string appears verbatim in some evidence entry's
`query`**; <= 6 iterations; the report round-trips through `json.dumps`; and
`usage.cache_read_input_tokens` is non-zero after the first call (if it is zero across
repeated calls, something in the tools+system prefix is varying).

The `memory` fault is the interesting one — the opening sweep finds nothing, so the loop has
to earn its second turn by going after `downstream_memory_bytes`.

**2. The `SRE_AGENT_LIVE` graph smoke test** — one gated run in
`agent/tests/test_smoke_live.py` asserting invariants only (terminates, produces a report,
citations resolve), never a specific diagnosis. Written but for the live suite; **not done**.

**3. Then sub-phase 3.3** — write tools + the deterministic policy gate.

**Still deferred (explicitly out of scope for 3.2):**
- Wiring the graph into `POST /investigate` as a background task, and persisting the report.
- LangSmith tracing (Phase 4).
- Raw-PromQL escape hatch, gated by an allow-list.
- The injector's `ground_truth_*` dual-write to Postgres — still only in
  `injector/ground_truth.jsonl`. Needed before Phase 5.
- `agent-api` `/metrics` + Alertmanager (existing tech debt).

**Note:** `CLAUDE.md`'s component table still lists Reasoning Loop and Orchestration as
"Not started", and its Current Phase section still points at 3.2. Update both once the live
rehearsal confirms the loop works.

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

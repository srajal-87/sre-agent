# HANDOFF.md — Session State

This file carries context between Claude Code sessions. Update it at the end of every session. Read it (alongside CLAUDE.md) at the start of every session.

---

## Last Session

**Date:** Day 13 (Phase 3 - sub-phase 3.3: write tools + the deterministic policy gate)

**What was done:**

Built 3.3 end to end across 17 TDD cycles and ran its live gate. **The write path is
proven: the agent diagnosed a live `bad_config` fault, proposed `toggle_config`, the policy
gate approved it, the action ran, and Prometheus confirms the fault cleared about a minute
before the injector would have reverted it.** Tests went 450 -> **684 passed, 20 skipped**.

Three commits on branch `phase-3.3-write-tools-policy-gate` (fast-forwards onto main):
`feat(tools)` the write layer, `feat(policy)` the gate, `feat(graph)` the wiring.

**The shape of it.** Three write actions (`restart_service`, `toggle_config`,
`rollback_deploy`) live in `agent/tools/actions.py`, in an `ACTIONS` registry that is
structurally separate from `TOOLS` - the model is handed the read-only investigation
surface and *never* an action, guarded by `test_actions_registry.py`. It proposes an action
by name in `update_hypothesis`, and a pure function in `agent/policy/` approves or denies.
A new `act` node sits between `decide` and `finalize` on the stop path, which is what makes
"one action per investigation" structural rather than a counter.

### The live rehearsal: four dry runs, then two with writes on

Dry runs first (writes off, the default), one per fault. **Every one was denied, each for a
different rule, and the stack was provably untouched** - container start times unchanged,
zero agent-written ledger rows.

| Fault | Diagnosed | Conf | Proposed | Gate |
|---|---|---|---|---|
| `bad_config` (data-service) | OK | 0.85 | `rollback_deploy` | denied - `blast_radius` |
| `latency` (downstream-dep) | OK | 0.85 | *nothing* | denied - `no_action_proposed` |
| `memory` (downstream-dep) | OK | 0.6 capped | `restart_service`/downstream-dep | denied - `incomplete_run` |
| `timeout` (api-gateway) | **wrong service again** | - | *nothing* | denied - `no_action_proposed` |

Then, with `--allow-writes` and `AUTO_ACTION_CONFIDENCE=0.85`:

- **Live approval.** `bad_config` -> diagnosed at 0.88 -> proposed `toggle_config` ->
  approved -> executed. `config_errors_total` climbs to 3641 and goes **flat at 10:59:25Z**;
  the injector's own revert was not due until ~11:00:25Z. The agent, not the injector,
  stopped it. That is the independent confirmation the plan asked for.
- **Live denial.** `timeout` on api-gateway with writes on: `escalate`, nothing run,
  api-gateway's container start time unchanged. Caveat below.

### Three findings, in order of how much they matter

**1. Bare action names are not enough, and the rehearsal is what proved it.** The plan had
the model propose from a schema *enum* only - names, no semantics - to avoid an answer key.
But the prompt asks for "the narrowest action that addresses the mechanism", which cannot be
judged from a name. Asked to remediate a configuration fault, the model proposed
`rollback_deploy`: sensible if you have just blamed a deploy, and always denied. The fix
(Step 17, added mid-rehearsal) draws a line the plan had collapsed: an **answer key** (this
fault -> that action) must never reach the model, but an action's **semantics** (what it
does) legitimately must. The `ACTIONS` descriptions - none of which names a fault type, and
a test holds that line - are now published into the `proposed_action` schema, built from the
registry so they cannot drift. The very next live run proposed `toggle_config` and was
approved.

**2. The plan's prediction table was wrong about the `timeout` case, and so is its rule
order.** It predicted `restart_service` on api-gateway denying on `blast_radius`. It denies
on `action_mismatch` first: `restart_service` does not address `timeout` in
`ACTION_ADDRESSES`. The order is right and the prediction was not - "this would not have
helped" belongs before "this is too risky", or you warn about the danger of something that
was never the right move. Two related reorderings against the plan's table, both with tests:
`unknown_action` before `blast_radius` (you cannot look up the radius of an action that does
not exist), and `blast_radius` before `low_confidence` (a property of the action and the
topology outranks one run's score).

**3. `AGENT_ALLOW_WRITES` is not a policy rule.** The plan listed it as gate rule #1, but
also wanted a four-fault dry rehearsal whose verdicts match the prediction table - and those
two cannot both hold, because rule #1 would make every dry run report `writes_disabled` and
prove nothing. The kill switch is enforced once, in `run_action`. The gate now reaches the
same verdict either way, and an approved dry run reads honestly: `auto_remediate`, with
`executed=False, dry_run=True` saying nothing happened.

### What is NOT closed

- **The 3.2 `timeout` miss is still open, and reproduced today** - the run pointed downstream
  again rather than at api-gateway. Per the plan's own precondition, `CLAUDE.md` and
  `ARCHITECTURE.md` were therefore **left untouched**: components 4, 5 and 6 still read as
  they did, and Stage 3 is not marked complete. Note the coupling: the gate acts on
  `hypothesis.service`, so a loop that names the wrong service hands the gate the wrong
  target - rule 6 (`target_mismatch`) limits the damage but does not fix the diagnosis.
- **A live `blast_radius` denial was observed only with writes OFF** (the `bad_config` dry
  run). With writes on, both denials landed on `no_action_proposed`. The gap is not
  meaningful - `act` never calls the executor on a denial, which is unit-tested and was
  confirmed live by api-gateway going untouched - but it is not the literal assertion the
  plan wrote, and a live blast-radius denial now needs the model to reach for a rollback,
  which Step 17's semantics actively discourage.
- **`latency` still proposes nothing**, even after Step 17. Plausibly correct conservatism
  rather than a defect: the prompt says proposing nothing "is the right one more often than
  not", and a log line reading "injected latency" does not obviously call for a restart.
  Worth characterising in Phase 5 rather than tuning blind.

### Two methodology traps, both of which bit today

- **A backgrounded `inject.py` that is killed when the shell exits never reverts and never
  writes its ground-truth row** - so the fault stays active and silently contaminates the
  next run. Two runs had to be discarded and redone. Wait on the injector before the script
  ends, and clear `/admin/fault` on all three services between runs.
- **`docker compose restart prometheus` between runs is necessary but not sufficient.** The
  fault duration must be shorter than the gap between runs, or run N's fault is still live
  during run N+1's baseline. 180s works with the recipe below.

**Tests:** agent **684 passed / 20 skipped** (was 450/20 - 234 new), injector 45, api 13
passed / 1 skipped. Six live investigations cost roughly $0.85. A transient network drop took
one run down mid-rehearsal, and the graph did exactly what it was built to do: `status=failed`,
a readable report, and the gate still consulted and recorded.

---

## Previous Session

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

## Earlier Session

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
- **The write actions are not in `TOOLS`, and the model can never call one.** `ACTIONS` is a
  separate registry; the model only *proposes* an action name in `update_hypothesis`, and
  `agent/policy/` decides. `test_actions_registry.py` holds both halves of that line: no
  action in `TOOLS`, and an action name reaching the model only as the proposal enum.
- **`AGENT_ALLOW_WRITES` is enforced in `run_action`, not in the gate.** The gate reaches the
  same verdict either way, which is what makes a dry run across every fault worth running. An
  approved dry run is `auto_remediate` with `executed=False, dry_run=True`.
- **`ok` vs `verification` vs `action_taken`.** `ok` says the action did its job;
  `verification` says whether the world got better (a restart that ran and left the service
  sick is `ok=True` with a verification saying so - the opposite of "could not restart", and
  they call for opposite next moves). `action_taken` is filled *only* when `executed` is
  true, so the investigations table never claims an action the system did not take.
- **Known limitation:** the sweep's metric and log calls use `lookback_minutes` (anchored to
  now inside the tool); only the deploy call is anchored to `reference_time`. Right for a live
  fault, wrong for replaying a hours-old incident. Fix is `since`/`until` on those calls.

## Next Session

**Pick up at: closing the `timeout` miss.** It is now the only thing standing between this
project and marking Stage 3 complete - 3.3 is built, tested and proven live, and 3.2's gate
is the one still open. It reproduced again on Day 13: the run pointed downstream rather than
at api-gateway. Until it is resolved, `CLAUDE.md` and `ARCHITECTURE.md` stay as they are.

It matters more now than it did on Day 12. The policy gate acts on `hypothesis.service`, so
a loop that names the wrong service hands the gate the wrong target. Rule 6
(`target_mismatch`) contains the damage - it refuses a proposal aimed at a service the
diagnosis did not blame - but a confidently wrong diagnosis aimed *consistently* at the wrong
service would pass that rule and be judged on the wrong topology.

**1. Characterise it before designing anything.** Re-run 2-3 times and see whether it lands
on `data-service` every time or only sometimes - that decides whether the fix is a prompt
change, a `decide` rule, or nothing at all. The recipe, with Day 13's two corrections baked
in (one fault, ~6 min):

```bash
set -a; . ./.env; set +a              # nothing in this repo loads .env
for p in 8001 8002 8003; do curl -s -X DELETE http://localhost:$p/admin/fault; done
docker compose restart prometheus     # clean 30m window; the sweep anchors to *now*
python injector/traffic.py --target api-gateway --rps 5 --duration 340 &
sleep 120                             # healthy baseline, so "this changed" is visible
python injector/inject.py --fault timeout --target api-gateway --duration 180 --no-deploy &
INJECT=$!
sleep 100                             # 60s rate window now fully inside the fault
PROMETHEUS_URL=http://localhost:9090 python -m agent.graph.run \
    --alert eval/scenarios/gateway-timeout.json --json > out.json
wait $INJECT                          # NEVER skip this - see below
```

Two traps, both of which cost runs on Day 13. **`wait` on the injector is not optional**: a
backgrounded `inject.py` killed when the shell exits never reverts the fault and never writes
its ground-truth row, so the fault stays live and silently contaminates the next run. And the
fault duration must be *shorter* than the gap between runs, or run N's fault is still active
during run N+1's baseline - 180s against this recipe, not 300s.

Then check against `tail -1 injector/ground_truth.jsonl`. The substantive question is
unchanged: the agent had evidence that data-service was *healthy* (p99 falling, no errors)
and named it anyway, on the strength of api-gateway's `"upstream timeout calling
data-service"` log string. Any fix should make the loop weigh gathered evidence against a log
message's implied blame - not just harden the prompt against this one sentence.

Note the deploy ledger decays: `seed_deploys.py --noise N --hours 2` must be re-run if more
than ~2h have passed, or `query_deploy_history` sees an empty window again
(`SWEEP_DEPLOYS_MINUTES` is 120).

**2. The `SRE_AGENT_LIVE` graph smoke test** - one gated run in
`agent/tests/test_smoke_live.py` asserting invariants only (terminates, produces a report,
citations resolve), never a specific diagnosis. Still **not done**.

**3. Two loose ends from the 3.3 rehearsal**, neither blocking:

- **A live `blast_radius` denial with writes ON** was never observed - both writes-on denials
  landed on `no_action_proposed`. It was observed with writes off. The mechanism is identical
  either way (`act` never calls the executor on a denial), so this is about completing the
  evidence, not about doubt.
- **`latency` proposes no action at all**, even with the Step 17 action semantics published.
  Possibly correct conservatism; do not tune it blind, characterise it in Phase 5 against the
  false-autonomous-action metric, which is exactly what that metric is for.

**Also worth folding in when convenient:** the `since`/`until` fix for the sweep's
now-anchored metric and log calls - already on the known-limitations list - is the most
likely lever on the temporal half of the `timeout` miss.

**Still deferred (explicitly out of scope for 3.3):**
- Wiring the graph into `POST /investigate` as a background task, and persisting the report.
  Note `investigations` has `action_taken` and `action_result` columns that the report now
  fills, and no column for `recommendation` or `policy_decision` - the latter is designed to
  nest into the `evidence` jsonb.
- LangSmith tracing (Phase 4). It cannot resolve from this host at present; the rehearsal ran
  with `LANGCHAIN_TRACING_V2=false` to keep the output readable.
- Raw-PromQL escape hatch, gated by an allow-list.
- The injector's `ground_truth_*` dual-write to Postgres - still only in
  `injector/ground_truth.jsonl`. Needed before Phase 5.
- `agent-api` `/metrics` + Alertmanager (existing tech debt).

**Note:** `CLAUDE.md` and `ARCHITECTURE.md` were deliberately **not** updated on Day 13, for
the reason the 3.3 plan itself set out: components 4, 5 and 6 flip to Complete, and Stage 3
closes, only once *both* the 3.2 and 3.3 gates are closed. 3.3's is; 3.2's is not. Those
files change only when the architecture genuinely does, and an open gate is not that.

**Running the write side by hand** (the 3.1 discipline, applied to actions). From the host,
export the per-service URLs first - `service_url()` defaults to compose names, which do not
resolve outside the network:

```bash
export API_GATEWAY_URL=http://localhost:8001 \
       DATA_SERVICE_URL=http://localhost:8002 \
       DOWNSTREAM_DEP_URL=http://localhost:8003
python -m agent.tools.probe --list-actions
python -m agent.tools.probe --action restart_service --args '{"service":"downstream-dep"}'
python -m agent.tools.probe --action restart_service --args '{"service":"downstream-dep"}' --execute
```

`--execute` writes even with `AGENT_ALLOW_WRITES` off, deliberately: that switch exists to
stop the *agent* acting on its own judgement, and here a person typed the action, the target
and the flag on one line.

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

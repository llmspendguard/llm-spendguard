# spendguard #7 — Learning advisor + temporal learning graph

Status: **design** (not yet built). This is #6 (record cost+quality) taken to **action**: a loop that
recommends **considering** history (not parroting it) and gets better as it runs.

## Principle: *considering* history, not *from* history
- **From history** = lookup: replay the past winner. Assumes the past transfers directly.
- **Considering history** = a **learning advisor** that uses history as *evidence/priors* and reasons over it
  **together with the current situation** — this task, today's prices, what changed (a model got cheaper, a new
  one exists), and outcomes from *similar* intents when this one is thin. It can recommend something **not in
  history**, and it **explains why** (with citations + a confidence). Layer 1 supplies the evidence; layer 2 is the judgment.

## Data model (SQLite, shared db)
1. **`calls`** — the hard corpus (have it, #6): per call/job → intent, chain, caller, model, cost, tokens, latency,
   prompt/output snippets, **quality + quality_confidence + quality_source**.
2. **`insights`** — curated learnings: `intent, lesson, evidence, source, confidence`. e.g. *"gpt-5.5 retype $149.76
   vs $33 — packing was the gap" (source: 2026-06-13 conversation, confidence 0.9)*. The advisor weights by confidence.
3. **`graph`** — a **temporal learning graph** (the new spine): nodes + timestamped, typed edges capturing how the
   evidence evolved and how the conversation/scripts drove it.

## The temporal learning graph
Most cost tools have a flat ledger. The graph turns it into a **story of learning with provenance**:
- **Nodes:** `run` (a job: model/cost/intent/date) · `decision` (chose gpt-5.5, packed 30) · `conversation_event`
  (a turn where a learning/decision emerged) · `script_version` (a script + git rev/diff) · `insight` · `outcome`
  (reconcile actual-vs-est, judge verdict, cancelled-waste).
- **Edges (temporal + causal):** `conversation_event —led_to→ decision —produced→ run —reconciled_to→ outcome
  —mined_into→ insight —influenced→ next decision`; `script_version —changed_by→ conversation_event` and
  `run —ran_with→ script_version`.
- **Why it matters:** (a) the advisor reasons over the *trajectory* ("our typing-cost understanding evolved:
  cancel-waste → packing → model-swap; each step's $/result"), not a snapshot; (b) **provenance** — every insight
  links to the conversation turn + script change + run that produced it, so it's auditable and re-mineable;
  (c) you can ask "what conversation produced the packing fix, and what did it save?" Stored in `graph_nodes` /
  `graph_edges` tables; optionally visualizable later.

## Backfill — a callable function *and* a Claude skill
Solves cold start; exposed three ways: `spendguard backfill` (CLI), `spendguard.backfill()` (fn, triggerable),
and a **Claude skill** (`SKILL.md`) so Claude can run/refresh it on demand.
- **Hard data (no LLM, no spend):** the real spend ledger — **OpenAI 2,956 + Anthropic 726 batches** → `calls`
  (model/cost/tokens/date). Quality from existing artifacts (e.g. `loinc_batch_judged.json` verdicts, apply outputs).
- **Quality reconstruction (the key unlock):** we didn't record quality live — but we can **mine it after the
  event**. An LLM reads the conversation window *after* a run + the **script/git evolution** (did the output get
  applied/committed = success; redone/abandoned/corrected = not) → a quality label **with a confidence score**.
  So quality on historical data is *recoverable*, not just forward-only.
- **Insights mining:** an LLM pass over conversations/memory → `insights` rows, each with a **confidence score**
  and a link back into the graph (provenance). Reviewable before they count.

## Backtest (validation)
Replay the advisor **as-of** a past date: "given the corpus up to 2026-06-09, what would `advise` recommend for
`concept-typing`?" → does it catch *pack it* (before the $149.76 run), *Opus is cheaper+better*, *don't cancel*?
Validates the advisor against decisions whose right answer we already know.

## Layers
1. **Deterministic evidence** — `$/good-per-config` per intent, **confidence-weighted** (judge > implicit-used >
   conversation-mined), + the graph trajectory. No LLM, no spend. `spendguard advise --intent X`.
2. **Learning advisor** — an LLM that reasons *considering* the evidence + insights + graph trajectory + current
   prices + the task → a recommendation **with rationale, citations, and a confidence**. Grounded strictly in the
   numbers we hand it; a **suggestion you still test** (feeds the existing test→estimate gate; never auto-applies).

## Caveats (so it informs, doesn't mislead)
- **Confidence everywhere** — every insight and reconstructed quality label carries one; the advisor weights by it
  and shows sample size.
- **Confounds** — a model can look better on easier items; advise reports the caveat and defers to a fixed-sample
  `compare` for apples-to-apples confirmation. History proposes, compare disposes.
- **No silent auto-apply** (especially prompts) — human-in-loop.
- **Privacy** — prompts/outputs + conversation mining are opt-in and local-only.

## Why this is worth getting right (for many, not just us)
Gateways do cost; eval tools do quality; nobody closes the loop into **cost-per-good-result that improves itself
with provenance**. A self-improving, auditable cost/quality advisor is a genuinely novel, broadly useful artifact.

## Meta-budget: spendguard governs its OWN LLM use
The advisor's Layer-2 LLM calls (quality reconstruction, insights mining, learning advisor, `optimize`)
must not become the runaway they exist to prevent. They are first-class spendguard-gated, with a
**separate budget, tag, and tracking** — build this cage *before* any Layer-2 LLM code.
- **Reserved intent namespace `spendguard:*`** (`spendguard:mine`, `:advise`, `:optimize`). The advisor
  sets `context(intent="spendguard:...")`, so every meta call is metered + logged like any other — but
  distinguishable. **Always tracked**, independent of the `calls.enabled` opt-in (the tool's own spend is never invisible).
- **Separate cap `caps.meta`** (default ~$2/day) enforced by the gate ONLY against meta-tagged spend,
  independent of `caps.daily`/`per_batch`/`realtime`. Meta can't eat the workload budget; a meta loop hard-stops at `caps.meta`.
- **Separate tracking** — `report` and `calls` break out a "spendguard meta" line so the advisor's cost
  never inflates your workload $ or $/good.
- **Cheap + bounded + estimate-first (dogfoods the protocol):** cheapest model (haiku/nano), sample +
  truncate inputs, a per-run hard cap on # LLM calls and $, INCREMENTAL (only mine new/unmined data,
  marked done), and it ESTIMATES its own cost and refuses over `--cap` before spending. The cost-control
  tool follows its own test→estimate→cap→reconcile gate.

## Build order
1. `calls` quality-confidence cols + `insights` + `graph` schema; **backfill** (hard: spend ledger + judge
   artifacts) as fn + CLI + **skill**; **deterministic `advise`**; **backtest** harness.
2. **Quality reconstruction** (LLM over post-event conversation + script/git evolution, confidence-scored) +
   **insights mining** (confidence-scored, graph-linked).
3. **Learning advisor** (LLM, considers evidence+insights+graph+prices+task) + `optimize`.

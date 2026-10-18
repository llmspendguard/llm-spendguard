# Feature: expose the executor / resolved-lane dimension on the spendguard MCP

## Why (the concrete gap, observed 2026-10-16)
I needed to answer one question: **"for intent `honestreview:precommit`, are the calls riding a $0
subscription lane or billing the metered API, and on which executor/lane?"**

- `spendguard_advise(intent="honestreview:precommit")` → gives cost **by model** (gpt-5.6-sol: 3281 jobs,
  $48.14; gpt-5.5: $3.20; claude-opus-4-8: $6.95; gemini-3.8-flash: $0.22). From the nonzero cost I could
  INFER "not on a $0 lane" — but it does not tell me the executor, the resolved lane, or the
  metered-vs-lane split.
- No MCP tool reports the realized `executor` / `resolved_lane` per call or per intent. The actual fact
  ("every recent precommit call is `executor=api, resolved_lane=api`, served `gpt-5.6-sol`, ~$0.015–0.024
  each") was only obtainable by a **direct sqlite query** on `~/.spendguard/spend.db` `calls`
  (`executor`, `resolved_lane`, `served_model`, `cost`, `ts`).

The ledger already HAS the columns (`executor`, `resolved_lane`, `served_model`, `requested_model`,
`redirect_reason`, `fell_from`, `fell_reason`, `cost`). They are simply not surfaced through MCP. This is a
read-only aggregation of data already captured — no new capture, no spend.

## What to build (pick the smaller of these that fits the existing surface)

### Option A (preferred): add a lane/executor breakdown to `spendguard_advise`
When `intent` is given, in addition to the per-model ranking, return a `routing` block:

```
"routing": {
  "by_executor": [
    {"executor": "api", "resolved_lane": "api", "jobs": 665, "cost": 16.12, "billed_jobs": 665,
     "zero_cost_jobs": 0, "models": ["gpt-5.6-sol"]},
    {"executor": "codex", "resolved_lane": "codex", "jobs": 1203, "cost": 0.0, "billed_jobs": 0,
     "zero_cost_jobs": 1203, "models": ["gpt-5.6-sol"]}
  ],
  "metered_share": 0.36,          // fraction of jobs (or of cost) that billed real $
  "on_zero_lane_share": 0.64,
  "window": "ts >= <lookback>"
}
```

Group `calls` by `(executor, resolved_lane)` for the intent over the advise lookback window; count jobs,
sum `cost`, count `cost==0` (zero-lane) vs `cost>0` (metered). This directly answers "is this intent on the
$0 lane or metered, and where is the money going."

### Option B: a dedicated tool `spendguard_route_realized(intent, since?, limit?)`
Returns the realized routing for an intent from the `calls` ledger (NOT a projection like
`spendguard_route_cost`, which models a hypothetical bulk job): per `(executor, resolved_lane, model)`
group — jobs, total cost, zero-cost count, and the most common `redirect_reason`/`fell_reason` when calls
fell off a lane to metered. Optionally a few sample rows (ts, served_model, executor, resolved_lane, cost).
This isolates the "did my routing actually land where I intended" question from the cost-ranking question.

## Hard constraints (match the repo's own rules — do not violate)
- **$0, read-only.** Pure aggregation over the existing `calls` table. No LLM call, no metered ping.
- **No hardcoding.** Column names, lane names, the lookback window: all from config / the data, never
  literals baked for one intent. The intent is a parameter; the tool must work for ANY intent.
- **ts is an ISO-8601 STRING, and `id` is NOT time-monotonic** (rows are backfilled/reconciled). Order and
  window by `ts` with string comparisons on zero-padded ISO-UTC values — NEVER by `id`, NEVER cast ts to
  an epoch number. (This bit me live: `ORDER BY id DESC` surfaced an August row among "newest".)
- **Split real-$ from est-value; never sum.** If the block reports value as well as billed cost, keep the
  metered/billed dollars separate from any subscription est-value, per the cost-display rule.
- **Degrade honestly.** If `resolved_lane`/`executor` is NULL for old rows, bucket them as `"unknown"` with
  a caveat — do not drop them and do not guess.
- Unique, semantic names for any new symbol; a new MCP tool name must not collide with existing ones.

## Verification
Run it for `honestreview:precommit` and confirm it reports the metered `api` bucket with nonzero cost that
matches the direct sqlite sum, and (after the precommit→codex-lane fix lands) shows the `codex`/zero-cost
bucket growing. A test should assert the aggregation equals a direct `GROUP BY executor, resolved_lane`
query on a fixture ledger.

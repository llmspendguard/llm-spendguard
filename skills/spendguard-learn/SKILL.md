---
name: spendguard-learn
description: Backfill spendguard's cost+quality corpus from real OpenAI/Anthropic batch history, then run the historical advisor and backtests. Use when the user wants to bootstrap spendguard from past spend, get a cost-efficiency recommendation for an intent, or validate the advisor against past decisions.
---

# spendguard-learn

Turn spendguard's recorded history into recommendations. All steps are read-only / no model spend
(except the optional Layer-2 LLM mining, which is opt-in).

## Backfill the corpus (from real spend, no spend to run)
```
spendguard backfill                              # OpenAI + Anthropic batch ledgers -> calls + graph
spendguard backfill --intent-map <dir|file>      # tag rows with intent (dir of *_batch_id.json, or {batch_id: intent})
```
Idempotent — re-running only adds new batches. Quality is mostly null (we didn't record it live);
cost-efficiency advice works immediately, quality accrues from feedback() and Layer-2 mining.

## Advise (recommend considering history)
```
spendguard advise                                # rank all models by cost-efficiency / $-per-good-result
spendguard advise --intent loinc-typing --plan gpt-5.5   # delta vs the model you're about to use
```

## Backtest (validate against known-good past decisions)
```
spendguard backtest --as-of 2026-06-09 --intent concept-typing
```
Replays the advisor as of a past date — check it would have caught *pack it*, *Opus-cheaper*, *don't-cancel*.

## In the process
Run `advise` at the **test** phase (before estimate/run) to pre-pick the model/packing, then still
test-small and confirm with `spendguard compare` on a fixed sample. History proposes; compare disposes.
Recommendations are suggestions — never auto-applied.

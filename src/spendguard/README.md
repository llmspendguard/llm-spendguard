# `spendguard/` — module map

The package. Zero required deps (SDKs are lazy-imported, optional); all state lives under
`$SPENDGUARD_HOME` (default `~/.spendguard`). Grouped by role.

### Core enforcement & pricing
| module | what it does |
|---|---|
| `gate.py` | The pre-spend GATE. Monkeypatches OpenAI `files.create`/`batches` + chat/messages create; estimates cost, hard-stops over the per-batch cap (interactive "allow?") and the cross-process daily/monthly cap; real-time accounting (incl. cached tokens); routes `spendguard:*` calls to the meta cage. `install()` / `register()`. |
| `pricing.py` | Canonical price table (`PRICING`), layered load (LiteLLM cache → `prices.json` → user override → fallback), `batch_cost`/`realtime_cost`/`estimate`/`normalize`, `cross_check_openrouter`, `freshness`. |
| `prices.json` | The shipped curated price table (edit here or `~/.spendguard/prices.json`; never hardcode in code). |
| `submit.py` | `guarded_submit` — estimate → enforce cap → log → submit, the one chokepoint for OpenAI batch jsonl. |
| `audit.py` | Guard: fail if any script hardcodes a price disagreeing with `pricing.py`. |
| `config.py` / `config_schema.py` | Settings resolution (env > file > default) + the declarative SETTINGS registry that drives `config`/`init`/SETUP/validation. |
| `budget.py` | SQLite cross-process ledger (charges) + daily/monthly/meta caps + `by_day`/`ledger_start`. |

### Accounting, reconcile, report, observability
| module | what it does |
|---|---|
| `reconcile_openai.py` / `reconcile_anthropic.py` | Actual billed batch spend from each provider (free GETs / local cache). |
| `report.py` | Daily/weekly/monthly spend + meta line + **ledger-leak alert** + **top learnings**; emailable. |
| `ledger_sync.py` | `reconcile-ledger` — local ledger vs provider billing → **leaks** (ungoverned spend). |
| `emit.py` | Best-effort event sinks: in-process callback, webhook, **OTel GenAI-convention** metrics+spans (→ Langfuse/Helicone/Phoenix). Never blocks the gate. |
| `notify.py` | Email delivery (Resend / SMTP). |
| `estimate.py` | Pre-flight job estimator (models × packing). |

### Learning advisor (cost + quality corpus)
| module | what it does |
|---|---|
| `calls.py` | Opt-in per-call corpus (intent/cost/tokens/quality); deferred quality (implicit "used" + `feedback`); `spendguard calls`. |
| `callio.py` | `fetch-io` — recover real prompt+output samples from providers (free, streamed) into a bounded `call_io` corpus → makes good%/$/good real. |
| `advise.py` | Layer-1 deterministic ranking by $/good (no spend); `backtest`. |
| `advisor.py` | Layer-2 caged LLM: `mine` (insights), `optimize` (recommendation), `reconstruct` (quality judge). |
| `learn.py` | `insights` (conditional, lifecycle-tracked) + the temporal learning graph (nodes/edges). |
| `validate.py` | Living insights — re-check vs current corpus (candidate→active→refuted/superseded). |
| `review.py` | Practice audit — was the usage *smart* (token-ratio, model-for-task), conditional insights. |
| `share.py` | Collective learning — opt-in scrubbed `insights export/import` (low-trust priors). |
| `backfill.py` | Seed corpus + graph from the batch ledgers (free). |
| `history.py` | `mine-history` — reconstruct intents from repo artifacts + causal graph edges + git signals. |
| `conv.py` | `mine-conv` — cached transcript index + caged synthesis of the cost playbook. |
| `models.py` | Per-model learnings (reasoning/cache/tokens), **auto-applied** on every call + self-heal + soft denylist. |
| `brief.py` | `brief` — "what we need to do" → pre-filled confirm-or-correct plan (the 6 fields) + advisor rec. |

### Efficiency lab & cost levers
| module | what it does |
|---|---|
| `experiment.py` | A/B/n lab — variants vs baseline, cost↓ **and** output-equivalence, graduated (pilot→kill→expand); `promote` (+`--batch`). |
| `equivalence.py` | Graded "same output?" ladder (exact→scalar→text; opt-in embed/rubric) + structural check. |
| `cacheaudit.py` / `cachetest.py` | Prompt caching — find reusable prefixes / empirically prove engagement + savings. |
| `semcache.py` | Opt-in response cache (exact + semantic) + batch `dedup` / `dedup-populate` (free re-runs). |
| `cascade.py` | Cost-aware routing — cheap→verify→escalate, denylist-aware. |
| `compare.py` / `adapters.py` | Run one prompt across models (cost/latency); provider adapters (OpenAI-compatible + Anthropic). |
| `sync.py` / `refresh.py` | Sync the price table from LiteLLM's published JSON. |
| `bootstrap.py` | Cold start — chain all the free mining + estimate the caged reasoning. |

### Entrypoints
| module | what it does |
|---|---|
| `cli.py` | `spendguard <command>` dispatch (see the README command reference). |
| `setup.py` | `init`/`config` (from the schema) + **`install-hook`** (gate another venv/repo). |
| `__init__.py` | Public API: `install`, `register`, `context`, `feedback`, `on_event`, pricing helpers. |

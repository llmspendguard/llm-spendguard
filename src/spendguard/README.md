# `spendguard/` — module map

The package. Zero required deps (SDKs are lazy-imported, optional); all state lives under
`$SPENDGUARD_HOME` (default `~/.spendguard`). Every `src/spendguard/*.py` module, grouped by role;
one-liners are read from each module's own top docstring.

### Core enforcement & pricing
| module | what it does |
|---|---|
| `gate.py` | The pre-spend GATE. Monkeypatches OpenAI `files.create`/`batches` + chat/messages create; estimates cost, hard-stops over the per-batch cap (interactive "allow?") and the cross-process daily/monthly cap; real-time accounting (incl. cached tokens); routes `spendguard:*` calls to the meta cage. `install()` / `register()`. |
| `pricing.py` | Canonical price table (`PRICING`), layered load (LiteLLM cache → `prices.json` → user override → fallback), `batch_cost`/`realtime_cost`/`estimate`/`normalize`, `cross_check_openrouter`, `freshness`. |
| `prices.json` | The shipped curated price table (edit here or `~/.spendguard/prices.json`; never hardcode in code). |
| `submit.py` | `guarded_submit` — estimate → enforce cap → log → submit, the one chokepoint for OpenAI batch jsonl. |
| `audit.py` | Guard: fail if any script hardcodes a price disagreeing with `pricing.py`. |
| `config.py` / `config_schema.py` | Settings resolution (env > file > default) + the declarative SETTINGS registry that drives `config`/`init`/SETUP/validation. |
| `budget.py` | The ONE writer into the `spend_events` ledger (`_record_spend_event`) + daily/monthly/meta caps (read via `_COUNTABLE`) + `by_day`/`by_dims`/`spent_since`. (Legacy `charges` retired → migration source only.) |
| `content_tokens.py` | How many INPUT tokens a request's content is worth — text plus images (PNG/JPEG/GIF/WebP dims) and PDF pages; the input side of the gate's estimate. |
| `provider_tokens.py` | Per-provider TEXT token estimation — a real tokenizer base × an agentically-chosen, self-calibrating per-provider factor; `spendguard tokens`. |
| `expected_output.py` | How many OUTPUT tokens to expect when the caller no longer says — the output side of the pre-spend estimate. |
| `bulkgate.py` | TEST-FIRST + ESTIMATE-FIRST enforcement — structurally BLOCK a bulk paid LLM job with no zero-spend estimate + verified small-sample test (+ eval); `spendguard maxtokens`. |
| `token_caps.py` | Guard — find every hardcoded output-token cap in the tree and make each answer for itself (a model's ruling, not a rule); `spendguard token-caps`. |
| `estimate_literals.py` | Guard — every cost function called with an integer literal, each adjudicated by a model so no magic number hides in the cost math; `spendguard estimate-literals`. |
| `estimate_divergence.py` | Enforce that a quote is GROUNDED — when a quoted price and the real bill disagree, say so by judgement rather than by rule; `spendguard estimate-divergence`. |
| `openai_batch_cli.py` | `spendguard batch-submit` / `batch-fetch` — the GATED, human-run halves of an OpenAI Batch-API job. |

### Accounting, reconcile, report, observability
| module | what it does |
|---|---|
| `reconcile_openai.py` / `reconcile_anthropic.py` | Actual billed batch spend from each provider (free GETs / local cache). |
| `report.py` | Daily/weekly/monthly spend + meta line + **ledger-leak alert** + **top learnings**; emailable. |
| `ledger_sync.py` | `reconcile-ledger` — local ledger vs provider billing → **leaks** (ungoverned spend). |
| `emit.py` | Best-effort event sinks: in-process callback, webhook, **OTel GenAI-convention** metrics+spans (→ Langfuse/Helicone/Phoenix). Never blocks the gate. |
| `notify.py` | Email delivery (Resend / SMTP). |
| `estimate.py` | Pre-flight job estimator (models × packing). |
| `scan.py` | `spendguard scan` — THE FIRST RUN: local transcripts only, zero config / keys / network / spend (safe via `uvx`). |
| `anomaly.py` | Daily spend ANOMALY detection (robust-z over the day series) — the automated gut-check that caught two 2× P0s. |
| `calibrate.py` | Learned cost calibration — correct naive job estimates from our OWN captured actuals; `spendguard calibrate`. |
| `coverage.py` | Audit which python venvs CAN make LLM calls but are NOT gated (the ungated realtime sources) + print the fix; `spendguard coverage`. |
| `guard.py` | Quantify spend GUARDED (cache hits, blocked calls, cascade, advisor, plan-vs-API) as a distribution + the decisions log; backs `spendguard savings`. |
| `focus_export.py` | Project `spend_events` into FinOps FOCUS 1.2 charge rows (an early FOCUS-for-LLM reference impl); `spendguard focus-export`. |
| `migrate_charges.py` | One-time migration — the legacy `charges` ledger (float $) → `spend_events` (integer micros, lifecycle, audit, Σ preserved); `spendguard migrate`. |

### Reconcile, realtime & multi-source attribution
| module | what it does |
|---|---|
| `reconcile.py` | The ONE reconcile loop — a single core + per-source `Source` ADAPTERS shared by every spend source (LLM batch + realtime, GPU, subscription, storage), replacing the one-off reconcilers; `spendguard reconcile all`. |
| `realtime_oracle.py` | Admin-usage realtime oracle — the historical realtime truth, timing-matched to OUR conversations per project (OpenAI/Anthropic hourly usage). |
| `resources.py` | Resource (non-LLM compute) spend — vast.ai GPU — tracked like LLM: mapped to org/team/project/contributor and pushed to the SAME server; `spendguard resources`. |
| `attribution.py` | Shared org → team × project classifier for work items from ANY source (chat, Claude Code, remote jobs) — one taxonomy, one caged classifier, so spend/value/work attribute the same way everywhere. |
| `truth.py` | Provider-TRUTH sync — push per-day provider totals to the org server; keys never leave this machine; `spendguard truth`. |
| `trust.py` | Trust check — cross-check the AUTHORITATIVE provider bill against what spendguard recorded and pushed, so a double-count or drift can't hide; `spendguard trust`. |
| `close.py` | Monthly CLOSE (client view) — the local half of the close statement; `spendguard close`. |
| `balances.py` | Per-vendor METERED prepay/credit balance ("how much is available") so routing can prefer idle sunk credit; `spendguard balances`. |
| `realized.py` | REALIZED efficiency — measured before/after $ per call around each insight's adoption (no counterfactuals); `spendguard realized`. |
| `sources.py` | `spendguard sources` — where CAN this machine spend (providers · agent tools · interpreters) and what do we already see? One free discovery, never reads your code. |
| `workdone.py` | Work-done layer — the CONTEXT for spend: per day/week/month/project, the WORK accomplished (git + batch intents), so spend reads as "spent $X, here's what got done"; `spendguard workdone`. |
| `verdict_ledger.py` | One store for "a model ruled on this, and here is what it said" — the shared verdict cache. |
| `tag.py` | Smart project tagging — the cascade that decides which project/work a charge belongs to (+ re-tag to fix cwd-fallback mistags); `spendguard tag`. |
| `chat.py` | claude.ai chat adapter (opt-in, on-device, macOS) — mine the session API into per-conversation spend + est-value + work; `spendguard chat`. |
| `saas.py` | SaaS client seam — the zero-dependency bridge from this local install to the (separate) spendguard server repo: push roll-ups/insights/calibration, pull commands/taxonomy/policy; `spendguard saas`. |
| `verify.py` | The forensic SELF-CHECK — is every path money can flow through actually CORRECT right now (model ids · failover · keys · economics)?; `spendguard verify`. |
| `signal.py` | Efficiency SIGNAL — the scrubbed, server-bound roll-up of "which work is worth the spend" per project·intent·model (cost + quality + waste + reco); no raw prompts leave; `spendguard signal`. |

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
| `bakeoff.py` | The EXPLORE half of the model-advisor — measure cost×quality for models you have NOT used on a job-type, so advise/recommend rank the whole universe, not just your history; `spendguard bakeoff`. |
| `measurement.py` | Receipts for a JUDGED number (bakeoff/eval good_rate) — the measurement twin of the spend receipt (sample/rubric/instrument, reconstructible); `spendguard measurement`. |
| `requirement_judge.py` | Judge an output by whether it meets THE PROMPT'S OWN success requirements — two-tier (derive the requirements, then verdict against them). |

### Efficiency lab & cost levers
| module | what it does |
|---|---|
| `experiment.py` | A/B/n lab — variants vs baseline, cost↓ **and** output-equivalence, graduated (pilot→kill→expand); `promote` (+`--batch`). |
| `equivalence.py` | Graded "same output?" ladder (exact→scalar→text; opt-in embed/rubric) + structural check. |
| `cacheaudit.py` / `cachetest.py` | Prompt caching — find reusable prefixes / empirically prove engagement + savings. |
| `semcache.py` | Opt-in response cache (exact + semantic) + batch `dedup` / `dedup-populate` (free re-runs). |
| `cascade.py` | Cost-aware routing — cheap→verify→escalate, denylist-aware. |
| `compare.py` / `adapters.py` | Run one prompt across models (cost/latency); provider adapters (OpenAI-compatible + Anthropic) — `adapters.call` is the gated call path (incl. `reasoning="best-value"`). |
| `sync.py` / `refresh.py` | Sync the price table from LiteLLM's published JSON. |
| `bootstrap.py` | Cold start — chain all the free mining + estimate the caged reasoning. |
| `prompts.py` | PROMPT-EFFICIENCY lint — mine the call corpus for waste (boilerplate, context stuffing, cheaper-model candidates), then hand each finding to the A/B lab; `spendguard prompts`. |
| `output_contract.py` | The SHAPE a job's output must arrive in — declared once, checked against a real sample before the bulk run. |
| `llm_files.py` | The ONE sanctioned way to put a file into an LLM prompt: whole, stamped, self-verified (never truncated). |
| `source_compact.py` | Shrink a source file for review WITHOUT splitting it — keep every line of code, drop the prose/comments (a token lever). |

### Subscription lanes & routing
| module | what it does |
|---|---|
| `lanes.py` | Subscription-lane ACTIVATION surface — exactly what stands between a user and their plans (+ `--probe` live check); `spendguard lanes`. |
| `lane_balance.py` | Load-balance across LANES by per-plan UTILISATION — sense HOT (shed-from) vs IDLE (absorb) plans + the model-proposed substitute set; `delegate` / `bulk_delegate`. |
| `lane_bandit.py` | Learned cross-lane ROUTER — a decaying contextual bandit ("equal use, then learn what's best for what, and relearn as models change"). |
| `lane_catalog.py` | Lane model CATALOG — the single source of truth for what each subscription LANE can invoke (use-names, tiers, fallback). |
| `lane_economics.py` | Subscription ECONOMICS — turn each plan's opaque "% remaining" into an absolute token CAP per window, tokens actually LEFT, effective $/token, and $ WASTED if the allowance expires at reset. |
| `lane_queue.py` | Durable LANE WORK QUEUE — accept work even when every lane is saturated, then DRAIN it onto idle plan capacity. |
| `lane_quota.py` | Lane QUOTA — the normalized cross-lane view of how much subscription quota each lane has left, + the shared cache that keeps reading each provider's quota surface cheap. |
| `lane_value.py` | Plan VALUE of subscription-lane calls with NO session-log miner (gemini/zai) — priced from the calls ledger; `spendguard lane-value`. |
| `dispatch.py` | The DISPATCH GOVERNOR — bounded concurrency + optional rate pacing per vendor/lane, so cross-LLM work at scale QUEUES instead of 429-storming. |
| `route_utility.py` | Routing UTILITY — one comparable score per target (a lane or a metered provider:model): drain the FREE plans first by real headroom, fall to the cheapest metered with prepay only when the lanes can't serve. |
| `catalog.py` | Live model-catalog CACHE — the served-list store so dispatch pre-flight grounds a model id against what a provider serves RIGHT NOW ($0), catching a stale id before a mystery 404; `spendguard sync-catalog`. |
| `model_preflight.py` | The FIXED, TESTED map that makes a model id CALLABLE-or-not answerable up front — catches a stale/renamed/unpriced id before a batch spends a cent; `spendguard preflight`. |

### Lane executors
| module | what it does |
|---|---|
| `codex.py` | Codex adapter — mine `~/.codex/sessions/**/*.jsonl` into est-value spend + work, incrementally; `spendguard codex`. |
| `codex_daemon.py` | WARM Codex lane over a persistent `codex mcp-server` — concurrent, multiplexed JSON-RPC. |
| `codex_exec.py` | Codex subscription lane — run spendguard's OpenAI-model meta prompts on the ChatGPT plan (Pro/Plus) via the Codex CLI; `spendguard codex-gc` prunes its shell-snapshot residue. |
| `zai_exec.py` | z.ai GLM Coding Plan lane — run GLM prompts on the flat-fee coding plan, not the metered z.ai API. |
| `subscription_exec.py` | Subscription executor — run spendguard's OWN meta prompts on the flat-fee Anthropic/Max plan, not the metered API. |
| `antigravity_exec.py` | Antigravity (Gemini) subscription lane — run Gemini-model meta prompts on the Google AI plan via the `agy` CLI. |
| `comprehend.py` | `spendguard comprehend` — fan a CORPUS of files across the $0 subscription lanes for comprehension / doc-mining / gap-analysis, instead of spawning Claude-only sub-agents. |

### Tiers, reasoning-equivalence & effort
| module | what it does |
|---|---|
| `tier_config.py` | Declare + VALIDATE the bulk-lane routing config — the surface that decides whether `bulk_delegate` can serve; `spendguard tiers`. |
| `reasoning_equivalence.py` | Canonical LANE↔METERED reasoning-equivalence map — the persisted truth for how a pinned (provider, model, reasoning) request is served on its $0 lane and on the SAME provider's paid API at equal-or-greater reasoning (never a different provider, never less reasoning). |
| `effort_titration.py` | Learn the CHEAPEST reasoning effort that HOLDS quality, per (intent, model); `spendguard effort-titrate`. |
| `best_value.py` | Resolve `reasoning="best-value"` to a concrete (model, effort) AGENTICALLY, from the measured learnings. |
| `reliability.py` | Lane + metered reachability sweep — prove every $0 lane and every keyed metered provider can actually SERVE a call (+ remediate); `spendguard reliability`. |
| `metadata_audit.py` | Health + drift audit of the MODEL-METADATA backbone (the published limits spendguard clamps to, the measured caps it raises within them) — the guard for two real silent failures; `spendguard metadata`. |

### Remote / GPU compute & provider adapters
| module | what it does |
|---|---|
| `gpu_port.py` | GPU-provider PORT — the explicit contract every remote-compute spend source implements + the ONE per-UTC-day cost-splitting math (vast.ai stays the reference adapter; every provider splits identically). |
| `remote.py` | Enforce the spend gate on DISTRIBUTED / REMOTE compute (vast.ai boxes, any SSH-reachable host) + sync their realtime logs back; `spendguard remote`. |
| `resource_state.py` | Per-resource STATE — the single persisted, multi-axis store of what is currently true about each lane and each metered (provider, model): cooldowns, size ceilings, proven-good — replacing the reason-blind in-memory flags. |
| `bedrock_adapter.py` | AWS Bedrock coverage — direct boto3 (patches `BaseClient._make_api_call`), records model-invocation usage into the SAME realtime ledger; capture-only, strictly fail-open; `install()`. |
| `lambda_adapter.py` | Lambda Labs GPU-cloud spend adapter (`gpu_port.GPUProvider`) — GET /api/v1/instances. |
| `modal_adapter.py` | Modal spend adapter (`gpu_port.GPUProvider`) — workspace billing via Modal's documented usage API. |
| `runpod_adapter.py` | RunPod GPU spend adapter (`gpu_port.GPUProvider`) — pods via RunPod's documented GraphQL API. |
| `vertex_adapter.py` | Google Gemini / Vertex coverage — direct google-genai SDK (patches `generate_content` + `embed_content`), into the same realtime ledger as `provider='google'`; capture-only, fail-open; `install()`. |
| `litellm_adapter.py` | LiteLLM coverage — capture spend for ANY LiteLLM-routed provider (Bedrock, Vertex/Gemini, Cohere, Mistral…) via its native success-callback, into the same realtime ledger; `install()`. |
| `provider_plugins.py` | Third-party provider plugins — the `spendguard.providers` entry-point group (load + list). |
| `provider_kit.py` | Conformance kit for provider plugins — importable by third-party test suites (`run_conformance` / `assert_conformance`). |
| `http_capture.py` | Raw-HTTP capture — spend that bypasses the SDKs (httpx / requests / urllib) becomes VISIBLE in the ledger, never blocked; `install()`. |
| `otel_ingest.py` | OTel GenAI span ingest — adopt the OpenTelemetry GenAI semantic conventions as the ledger interchange format, so a call traced by any OTel instrumentation lands as a `spend_events` row without re-instrumenting; `spendguard otel-ingest`. |

### Conversation attribution & client surfaces
| module | what it does |
|---|---|
| `claudecode.py` | `spendguard claude-code` — mine `~/.claude` transcripts into `spend_events`: per-turn est-value (`source="claude-code"`, `kind="est_chat"`, billed=0) + observable overage reconciled to real $ (`claude-code-overflow` → `anthropic-invoice`); per-conversation/context views labeled by sidebar title. |
| `compaction.py` | Compaction lifecycle — the agentic `/compact` advisor (`compact --tailor`) + the PreCompact preservation-guidance / SessionStart(compact) hooks that measure the real before/after ratio (cuts the ~19% auto-compaction loss). |
| `mcp_server.py` | `spendguard mcp` — a stdlib-only stdio MCP server exposing **9 tools** (4 model-advisor + 5 read-only spend/compaction); `install-mcp` registers it in `~/.claude.json`. |
| `receipt.py` | The inline spend tally (per-flow receipt + status-line footer) and the Claude Code hooks it installs (`install-receipts`: statusLine + Stop + PreCompact + SessionStart). |
| `ledger.py` | The `spend_events` / `spend_audit` forensic ledger — the single money-of-record every budget writer records through. |

### MCP / cross-LLM / serve surfaces
| module | what it does |
|---|---|
| `crossllm.py` | `spendguard.ask` / `spendguard ask` — the ONE stable way to run a cross-LLM query, so external callers never reach into `vendor_call` internals to fan out across models. |
| `serve.py` | `spendguard serve` — the cross-LLM ask surface over localhost HTTP, so ANY tool or language (not just Python) can run an honest, gated, governed cross-LLM query. |
| `vendor_call.py` | ONE gated entry point for calling any vendor — a TYPED outcome, a TOTAL deadline, and a fan-out that cannot report a consensus it did not get (`call` / `fan_out` / `first_ok` / `consensus`). |
| `deid.py` | Client-side DE-IDENTIFICATION of the small amount of text that leaves this machine (scrub before it hits a lane or provider). |

### Entrypoints
| module | what it does |
|---|---|
| `cli.py` | `spendguard <command>` dispatch (see the README command reference). |
| `setup.py` | `init`/`config` (from the schema) + **`install-hook`** (gate another venv/repo) + `install-skills`/`install-rule`/`gate-coverage`. |
| `__init__.py` | Public API: `install`, `register`, `context`, `feedback`, `on_event`, pricing helpers (+ `install_litellm`/`install_bedrock`/`install_vertex`). |
| `__main__.py` | Enable `python -m spendguard …` — identical to the console script, but works even where the console script isn't on PATH (e.g. an ephemeral GPU box). |
| `runner.py` | `spendguard run -- <command>` — gate a process WITHOUT touching its interpreter (child `PYTHONPATH`; the default way to gate since 0.8). |
| `schedule.py` | `spendguard schedule` — wire the OS-native scheduler (launchd / cron / schtasks) to run the roll-up on a cadence; idempotent + removable. |
| `ui.py` | Tiny shared, zero-dependency CLI UI helpers (e.g. the `estimate_only` confirm prompt). |

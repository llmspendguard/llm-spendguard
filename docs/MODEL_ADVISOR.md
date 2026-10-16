# The model-advisor — data-driven, agentic model selection

**The question:** for THIS kind of job, which model is the most cost-effective at the quality it needs? With
hundreds of models and prices that change weekly, the honest answer is not a vendor's benchmark — it is *your
own* measured usage. spendguard already records the cost, tokens, and (judged) quality of every call it gates;
the model-advisor turns that corpus into a recommendation, and exposes it over MCP so any agent working in a
repo can ask.

It is available three ways: the `spendguard advise` / `bakeoff` CLIs, the `spendguard mcp` server (below), and
directly as Python (`advise.ranked`, `advisor.recommend_models`, `bakeoff.bakeoff`).

For the no-login global model reference, see [llmspendguard.com/catalog](https://llmspendguard.com/catalog). Its
`as_of` is the authored row price/capability-change date—not this machine's LiteLLM cache fetch time or the server's
ingest time.

## The MCP tools (`spendguard mcp`)

`spendguard mcp` is a stdlib-only MCP **stdio** server — no SDK dependency (like `serve.py`), client-side and
per-user (it reads THIS machine's local ledger, not the SaaS server). Point any MCP client at the command
`spendguard mcp`. `initialize` returns self-documenting `instructions`; `tools/list` carries each tool's schema.

The server currently exposes 21 tools. The table groups the exact names registered in `mcp_server._TOOLS`; use
`tools/list` as the live schema source.

| tool | spends? | what it does |
|---|---|---|
| `spendguard_advise(intent?, plan?, as_of?)` | **$0** | Rank models you have ALREADY used for a job-type by cost-effectiveness at the quality they held — `$/good-result` where quality is labeled, else `$/M output`. Returns the ranked models, the pick, and caveats. |
| `spendguard_models()` | **$0** | The actionable catalogue: curated + your verified prices, each with per-1M rates + provider. |
| `spendguard_recommend(intent, k?, quality_bar?, budget_usd?)` | small, meta-capped | Agentic top-K on the cost×quality frontier: the reasoner infers how much precision the job needs (or honors `quality_bar`) and ranks the CHEAPEST models that MEET the bar, each with the measured `$/good`. Estimates first; an optional `budget_usd` adds an explicit refusal ceiling. |
| `spendguard_bakeoff(intent, candidates, prompts?, sample_n?, budget_usd?)` | real, metered | Measure cost×quality for a SLATE of untried models on a sample of the intent's tasks; judge each output and RECORD it, so they appear in advise/recommend afterwards. **No `budget_usd` → returns the ESTIMATE only, never auto-spends.** |
| `spendguard_stability(...)` | plan usage | Measure run-to-run outcome variance at production settings. |
| `spendguard_vision(...)` | metered | Estimate-first governed vision call; no `budget_usd` means estimate only. |
| `spendguard_delegate(...)` | dry-run by default | Classify and offload a self-contained agentic task to a configured provider plan. |
| `spendguard_route_cost(...)` / `spendguard_route_horizon(...)` | **$0** | Compare true lane/batch/realtime economics for one bulk intent or a whole job horizon. |
| `spendguard_plan_queue()` / `spendguard_dispatch_state()` | **$0** | Inspect predictive batch planning and live admission/queue state. |
| `spendguard_run_jobs(...)` | estimate-first | Plan a whole job set; a goal budget is required to execute. |
| `spendguard_health(...)` | estimate or probes | Bounded lane/provider reachability; metered probes can cost a small amount. |
| `spendguard_savings()` | **$0** | Report guarded savings as the separate third cost axis. |
| `spendguard_config()` / `spendguard_version()` | **$0** | Read resolved non-secret configuration and served-vs-green release state. |
| `spendguard_spend_overview()` | **$0** | The headline for THIS account: REAL $ out the door (subscription base + Claude Code overage + API credits) shown SEPARATELY from est-value (plan-covered Claude Code usage). The two axes are **never summed**. |
| `spendguard_overage_status()` | **$0** | Are we on PAID overage right now — the weekly subscription cap is hit and this account is billing per-token — from observable transcript signals, plus reconciled real overage $ by month. |
| `spendguard_top_conversations(by?, limit?)` | **$0** | Rank Claude Code conversations by est-value (default) or by real overage $ (`by="overage"`), each labeled by its sidebar title. Answers "what used the tokens". |
| `spendguard_conversation_cost(conversation_id)` | **$0** | Cost of ONE conversation by its transcript id: plan-covered est-value + observed overage upper bound, kept as separate axes. |
| `spendguard_compaction_candidates(limit?)` | **$0** | Open conversations expensive to keep alive (large re-read context every turn) with the $/turn cost, what compacting would save, and the ready-to-paste effective /compact command. |

## The loop

```
                 ┌──────────────────────────────────────────────────────────┐
   your gated    │  ledger (calls corpus): per-(intent, model) cost, tokens, │
   LLM usage ───▶│  latency, and JUDGED quality (good%)                      │
                 └───────────────┬──────────────────────────────────────────┘
                                 │  advise.ranked  (deterministic, $0)
                                 ▼
        EXPLOIT:  spendguard_advise ── rank what you've used by $/good
                                 │
                                 │  advisor.recommend_models  (agentic, meta-capped)
                                 ▼
        DECIDE:   spendguard_recommend ── intent quality-bar + top-K on the frontier
                                 │
                                 │  a model you've NEVER run has no evidence …
                                 ▼
        EXPLORE:  spendguard_bakeoff ── run a slate on a task sample, JUDGE, RECORD
                                 │
                                 └──────────▶ (writes back to the corpus) ──▶ advise/recommend now include it
```

**EXPLOIT** ranks recorded evidence; **EXPLORE** (the bakeoff) is the only honest way to get evidence for a
model you have never run — there is no free lunch, only a cheap, gated, sampled one. The bakeoff records its
results into the same corpus advise reads, so the two converge: each bakeoff sharpens the recommendation.

## The rails (every path)

- **Agentic decisions.** "How good does this job need to be" (the quality bar) and "is this output good" (the
  bakeoff judge) are LLM judgements, never keyword rules. The judge returns a **structured** `{"good": bool}`;
  ambiguity is UNLABELED, never guessed.
- **Estimate-first.** `recommend` computes its estimate before its small meta-capped synthesis and honors an optional
  `budget_usd` ceiling. `bakeoff` over MCP will not run at all without an explicit `budget_usd`; omission returns its
  zero-spend estimate.
- **$0 lanes first.** The bakeoff fan-out and the reasoner ride the subscription lanes ($0) where available,
  metered API otherwise.
- **Recorded, never re-paid.** Bakeoff results and quality labels persist to the base sqlite.

## The consumer contract: `adapters.call`

The advisor (and any tool that calls an LLM through spendguard) uses `adapters.call`. Its request/response is
documented so a consumer never has to reverse-engineer it. It returns a `CallResult` (a `dict` subclass whose keys
are also attributes) for success and ordinary failure. Deliberate `SpendGateRefused` subtypes propagate by design:

- **Request:** `call(model, prompt, max_tokens=None, system=None, reasoning=None, schema=None, timeout_s=None,
  sig=None, intent=None, files=None, images=None, no_metered_fallback=False, no_substitution=False,
  measurement=False, metered_only=False)`. `timeout_s` is a client-side cancel that actually stops the call **and** its billing (lane
  AND api). `schema` forces structured output. `intent` (alias for `sig`) tags the call's job-type for attribution —
  **a paid call with NO intent lands in `(none)`, so always pass one.** `no_metered_fallback` makes a lane miss on an
  UNSUITABLE task (empty / off-shape / oversized / transient-quota) an error, never a paid retry — $0. The ONE exception
  is a lane that is infrastructure-DOWN (auth expired / CLI crash / rejected model): a down lane still fails over
  (another $0 lane first, then metered) so work is never lost to a dead lane — a STRUCTURAL split on the miss reason, not
  a parse of the error prose (decided with the maintainer 2026-09-26). `no_substitution=True` PINS the named model (the utilisation bandit
  can't swap it). `metered_only=True` forces the **faithful metered half** of the atomic (lane, metered) pair — it
  SKIPS the $0 lane and rides the provider API at equal-or-greater reasoning (`reasoning_equivalence`); use it for a
  distribution-sensitive / verdict-cached fan (a refuter, a cached classifier) that must reproduce its serial
  distribution regardless of which host served it. `reasoning="best-value"` lets spendguard pick the cheapest
  (model, effort) whose measured quality holds for `intent`.
- **Unnamed models:** `model=None, measurement=True` raises `ModelSelectionContractRefused`. A labelled call with
  explicit best-value, or with configured `advisor.default_reasoning="best-value"`, delegates selection to the
  measured router. Every other unnamed call uses configured `advisor.default_model`, else the catalog's cheapest
  priced chat model (currently `gpt-5-nano`), which is a metered API default and not a lane daemon. A missing priced
  catalog default is refused rather than replaced by a hardcoded id.
- **Response keys** (same on success and failure):
  - `text` — the answer, or None on failure / truncated-past-retry.
  - `cost` — $ for this call: **0.0 = a $0 subscription lane**, positive = metered API, None = refused/errored.
  - `executor` — **WHICH path**: a lane name (`claude-code`|`codex`|`gemini`|`zai-coding`) or `api`. This is how
    a caller tells lane-vs-API from the result alone, on success AND on error.
  - `in_tok` / `out_tok` / `latency` / `finish_reason` (`length` = truncated) / `substituted_from`.
  - on FAILURE: `error` (one line), `error_type` (the exception CLASS — `APITimeoutError` [deadline] vs
    `APIConnectionError` [transport] vs `NotFoundError` [bad model]), `status_code`, `provider_error` (the real
    response body), `cause` (the underlying error behind a generic wrapper — `'Connection error.' ←
    'ConnectTimeout'`), `retry_after`.

## Wiring the MCP server into a client

**One command:** `spendguard install-mcp` registers `spendguard mcp` as a stdio server in `~/.claude.json` (a
top-level `mcpServers` entry — exactly how symgrep / 7thsense / ccwatch are registered), so all current tools are
reachable from every repo; `--remove` unregisters it. Restart Claude Code (or reconnect MCP) to pick them up.

To wire it by hand into any other client, register the command `spendguard mcp` as a stdio MCP server. It must run
under the **gated** interpreter (the spending tools `require()` the gate and fail closed otherwise); the read-only
tools (`advise`, `models`, and the spend/compaction queries) do not spend and need no gate.

## Cross-check (doc ↔ code)

Every claim above maps to code, and this table is the checklist to re-verify when either changes:

| claim | code |
|---|---|
| 21 tools, names and schemas | `mcp_server._TOOLS` |
| advise ranks $/good, else $/M-out | `advise.ranked` (shared by the CLI printer and the MCP tool) |
| recommend: agentic bar + top-K, estimate-first, schema-forced | `advisor.recommend_models`, `_REC_SYS`, `_REC_SCHEMA` |
| bakeoff: sample → run slate → structured judge → record → re-rank | `bakeoff.bakeoff`, `_judge_one` (`_JUDGE_SCHEMA`), `calls.insert`, `advise.ranked` |
| bakeoff over MCP never spends without `budget_usd` | `mcp_server._tool_bakeoff` |
| response contract (executor/cause/…) | `adapters.call` docstring; `_call_once` success + except returns; `_exc_cause` |
| stdio JSON-RPC, `handle()` pure | `mcp_server.handle` / `serve_stdio` |

Guards: `tests/test_mcp_server.py` (tool surface + the protocol frame), `tests/test_advise.py`,
`tests/test_advisor.py`, `tests/test_adapters_error_transparency.py`.

# Changelog

All notable changes to **llm-spendguard**. Format loosely follows Keep a Changelog; dates are UTC.

## [Unreleased]

### Fixed
- **Codex lane cold-start — `codex exec` now skips the two per-call setup costs (>75s → single-digit seconds).**
  MEASURED 2026-08-19: a real one-shot `codex exec` took >75s because each call re-set-up (a) the writable-workspace
  sandbox and (b) all enabled plugins/MCP servers. A headless completion needs neither, so `codex_exec.run_prompt`
  now passes `-s read-only` and disables every enabled plugin for the invocation (`_plugin_disable_flags` reads the
  live `~/.codex/config.toml`, no hardcoded names; `-c 'plugins={}'` does NOT work — the per-plugin tables win).
  Clean runs drop to ~3–7s (gpt-5.5 and the default codex model both answer). NB: residual per-call variance remains
  (periodic marketplace/session refresh), so for *reliably* fast + $0 codex delegation the real fix is a **warm codex
  daemon** (`codex mcp-server` / `app-server daemon`, set up once → ~sub-1–2s) — a separate build; until then codex
  stays out of the default `delegate` lane set, and gemini(low)/zai remain the reliable fast lanes.

### Added
- **Lane visibility & value — subscription lanes are now RECORDED by name, PRICED, and SHOWN inline.** The inline
  receipt (the Claude Code Stop-hook line the desktop app surfaces each turn) previously showed only totals — you
  could not see WHICH plan served your work, and two of the four lanes' plan value was invisible. Now:
  - **Recorded:** every lane-served call stores its `executor` (claude-code / codex / gemini / zai-coding) and
    `project` on the ledger row — the `calls` table gains two migrated columns (backward-compatible ALTER). A stored
    fact, not a provider-guess. (`calls.record(..., executor=, project=)`, passed from the adapter lane path.)
  - **Priced:** lanes with NO session-log miner (gemini, zai-coding) are now valued from the ledger — new module
    `lane_value` prices their `kind='subscription'` calls at API-equivalent rates (`pricing.realtime_cost`) and
    stamps per-lane est-value, so their plan value stops reading as $0 (which also un-blinds the load-balancer's
    utilization brain). WHICH lanes are ledger-valued is DERIVED — every lane minus those a session miner already
    covers (`receipt._SOURCE_REFRESH`) — never a hardcoded list. New command `spendguard lanevalue`; auto-refreshed
    (staleness-gated, ~$0) on the receipt render path.
  - **Shown:** `render_line` (the one-line widget) and the footer name the lanes serving your work
    (`… :: est value $X/mo · lanes: codex 12× · gemini 3× ($0)`); the full `spendguard receipt` splits **plan value
    by lane** and lists which lanes served the work. The two axes are still never summed — lane usage is $0 billed,
    plan-served, and the est-value split sits on the est-value axis only.
  Guarded by `tests/test_lane_visibility.py` (persists executor+project; prices ONLY miner-less lanes, never
  double-counting the session-mined ones; renders the lane inline in the widget, footer, and full table).
- **Warm Codex lane via a persistent `codex mcp-server` (`codex_daemon`) — the GPT/Codex plan is now a fast $0
  delegation lane.** Instead of cold-starting `codex exec` per call (>75s), spendguard reuses ONE `codex mcp-server`
  per process (set up once; each request is a warm `tools/call`). PROVEN live: `delegate(lanes=['codex'])` →
  gpt-5.5, **$0 on-plan, 5s** (was >75s / intermittent hang). **Self-healing** — `ensure_running()` lazy-starts and
  restarts a dead server, `atexit` tears it down (the "starts when spendguard is there, restarted as needed" ask).
  **Context-capable** — the `codex` tool returns a `threadId`; `codex_daemon.run_warm(thread=…)` uses `codex-reply`
  to CONTINUE the conversation (proven: recalled a prior word), so a series of delegations can build on each other.
  Wired into the codex lane behind `advisor.codex_daemon` / `SPENDGUARD_CODEX_DAEMON` (default OFF in code — the
  per-call exec stays the safe default; armed in the user's config). Guarded by `tests/test_codex_daemon.py`.
  (Cross-*invocation* warmth would use `codex app-server daemon`, which needs the standalone-codex install —
  documented upgrade.)
- **`lane_balance.delegate(task)` + `spendguard lanes --delegate "<task>"` — offload work to an idle plan at $0.**
  From an orchestrator that itself can't move plans (e.g. a Claude Code session — it *is* Claude), delegate one task
  to the cheapest VIABLE idle subscription lane and get the answer back: the heavy tokens run **$0 on the idle plan**,
  the orchestrator spends only coordination. Picks from `advisor.delegate_lanes` (default `gemini`, `zai` — **codex
  EXCLUDED**, its CLI is agent-slow), **least-utilised first**, at **low** reasoning (gemini-high returns empty);
  EMPTY or errored output **falls through** to the next lane, and a metered-API answer is flagged `billed=True`
  (never a silent charge). Proven live — a task ran **$0 on `gemini-3.7-flash-low` from inside a Claude session**.
  Guarded by `tests/test_delegate.py`.
- **Selectable reasoning on the Codex lane — `codex_exec` `reasoning=` → `-c model_reasoning_effort`.** The Codex
  plan model's reasoning scale is `none|low|medium|high|xhigh|max` and has **no `minimal`** (MEASURED 2026-08-19:
  sending `minimal` is a hard 400) — a concrete instance of the cross-provider naming gap. `codex_exec.run_prompt`
  now takes `reasoning=` and threads `-c model_reasoning_effort=<v>` with the standard ordinal mapped to that scale
  (`minimal→none`; the rest pass through, codex validates). `reasoning` is now a protocol-uniform arg on all four
  lane `run_prompt`s (Gemini's effort rides its model suffix; Claude/z.ai accept-and-ignore for now), threaded from
  the one `adapters._call_once` lane call site. Guarded by `tests/test_codex_reasoning.py`. (NB: measured — even at
  codex 0.148.0 with `low`, `codex exec` on a real prompt stays >75s; the CLI *agent* overhead, not reasoning, so
  gemini(low)/zai remain the fast $0 delegation lanes.)
- **Standard cross-provider reasoning knob (OpenAI half) — `models.normalize_reasoning`.** One ordinal
  `reasoning=minimal|low|medium|high` now maps to each OpenAI model's VERIFIED effort value, hiding the gpt
  family's inconsistent naming (gpt-5.5 wants `none`, gpt-5-mini/nano + o-series want `minimal` — only the FLOOR
  varies; low/medium/high are the API's universal values). Non-reasoning models drop the param (no 400). Wired
  into the OpenAI send path. Anthropic (thinking BUDGET, conflicts with a forced-tool schema) and Gemini (model-id
  SUFFIX) use different mechanisms and return None here until MEASURED — never guessed (the models.py doctrine).
  Guarded by `tests/test_reasoning_normalize.py`. Learned-best-default, cost-per-level, and the Anthropic/Gemini
  halves are the next increments (a measured pass, which also exercises those lanes).
- **Load-balancing completed — EFFECTIVE-UTILISATION routing + REACTIVE failover (`lane_balance` + `adapters`).**
  `route_decision` now aims at effective utilisation rather than only saturation: it proactively routes an intent to
  the LEAST-utilised acceptable plan whenever that plan sits more than `advisor.lane_balance_margin` below the
  primary's utilisation — so the idle paid plans actually get FILLED, not just used as a safety valve. And REACTIVE
  failover is wired: when a lane FAILS (plan exhausted), `_call_once` routes to a confirmed substitute PLAN *before*
  the metered API, cools the failed lane, and records the substitution — a one-hop `_sub_guard` (thread-local) stops
  a substitute from itself substituting. New config: `advisor.lane_balance_margin` (routing sensitivity),
  `advisor.lane_models` (candidate model per plan), `advisor.lane_idle_ratio`/`lane_hot_ratio` (display). Needs
  `advisor.executor = pool` (all lanes active) so a substitute lands on the idle plan, not the metered API. Guarded
  by `tests/test_lane_substitution.py` (now incl. the reactive-dispatch path).
- **Cross-plan substitution — an idle plan absorbs a hot plan's work (`lane_balance` + `adapters`, Part 2 stages
  2–3).** When a call's INTENT has a CONFIRMED substitute and its primary plan is HOT while an acceptable substitute's
  plan is IDLE, `_call_guarded` now runs the substitute model instead — resolved through the SAME guarded path so it
  gets its own output budget + input check, and RECORDED as the model that answered (with `substituted_from`
  provenance; never silent). Authorization is **model-proposes-you-confirm-once**: `propose_substitutes()` — an
  agentic cheap-model judge decides which idle-lane candidate models are acceptable for the intent → PENDING — then
  `confirm_substitute()` makes one usable. `route_decision()` is PURE (registry + utilisation, no LLM in the hot
  path), never routes onto a cooling lane, and is **default OFF** (no confirmed substitute → every call unchanged;
  proven by the full suite passing with the hot-path change in place). Stage 3: `adapt_system()` agentically rewrites
  the instruction for the target model WITHOUT changing the task, recorded per (intent, target) and applied
  mechanically by dispatch (`prompt_adapted` flagged) — and since a substitute on a new model is a new sig, the eval
  gate still validates it before scale (adaptation can't quietly change the task). CLI: `spendguard lanes --balance |
  --propose <intent> <model> | --confirm <intent> <substitute>`. Candidates come from config `advisor.lane_models`
  (no hardcoded model list). Guarded by `tests/test_lane_substitution.py`. Remaining increment: reactive
  lane-exhaustion failover (route on a lane error, not only proactively).
- **Proactive lane-utilisation brain (`lane_balance`) — stage 1 of load-balancing across subscription plans.**
  `lane_utilization()` reports per-plan **est-value ÷ plan fee this month** so the coming router — and the user via
  `format_utilization()` / (planned) `spendguard lanes --balance` — can see which flat-fee plans are **HOT** (shed
  from) vs **IDLE** (absorb overflow): on the live ledger, claude-code **9.98×** vs codex/gemini/zai **0.00×**.
  Reuses the receipt's own per-source cache + re-windowing, so the numbers MATCH the receipt and inherit its
  stale-cache guard. HONEST by construction: this is est-VALUE utilisation, **not** the provider's true remaining
  quota (Anthropic Max weekly/5h limits aren't API-exposed) — a capacity-pacing signal, with the reactive lane
  error as the hard exhaustion backstop. Thresholds are config (`advisor.lane_hot_ratio` / `lane_idle_ratio`);
  per-lane fee is exact via `subscription.lane_plans` else an even split of the plan total (flagged, never shown as
  exact). Sensing only — the routing decision, the `_call_once` dispatch wiring, and the model-proposes-you-confirm-
  once substitute registry are the next stages. Guarded by `tests/test_lane_balance.py`.
- **Lifecycle EVAL gate — the quality checkpoint above the shape-test (`bulkgate`).** The test-first gate now
  enforces the full **estimate → test → EVAL → run** lifecycle: a scale run (est ≥ `gate.bulk_min_usd`, default
  lowered 0.50 → **$0.25**, AND multi-unit so there is a sample) is authorized only when the call-class sig has a
  fresh estimate, a shape-verified test, AND a fresh **passing eval** — a STATED bar plus an **agentic verdict** on
  the test sample (an LLM judge = `config.advisor_judge_model` / `gate.eval_model`, caged as the `spendguard:eval`
  meta intent). The eval VERDICT is a judgement (the LLM decides it, never a keyword); the gate's CHECK ("does a
  fresh passing eval exist?") is the only mechanical part. A bar is REQUIRED (an empty bar is refused — no
  rubber-stamp); an unparseable judge reply is FAIL-SAFE (treated as FAIL); a failing eval keeps scale blocked until
  a passing one exists (iteration is free). New surface: `record_eval` · `eval_job` · `gated_batch().eval(bar=…)`;
  new columns on `gate_ledger` (additive). Config: `gate.require_eval` (default on; set false to keep the
  estimate+test-only gate while a repo adopts evals) and `gate.eval_model`. Rollout unchanged —
  `SPENDGUARD_ENFORCE=warn` (default) logs would-block, `block` enforces. Guarded by
  `tests/test_lifecycle_eval_gate.py` (offline gating) and proven end-to-end by `scripts/lifecycle/demo_eval_gate.py`
  (a real Haiku judge PASSES a good sample and FAILS a bad one against the SAME bar — an agentic verdict, not a
  rubber-stamp).
- **Gemini subscription lane** — spendguard's own Gemini-model meta prompts can ride the Google **Antigravity**
  plan (the `agy` CLI) at $0 billed, mirroring the existing `claude-code` and `codex` lanes: `GEMINI_API_KEY` +
  `GOOGLE_API_KEY` are stripped from the child so a plan call can never silently become a metered charge, usage
  is read by field name, and any failure (CLI missing, quota, parse mismatch) degrades to the metered API — the
  lane can break, the advisor cannot. Set `advisor.executor` to `gemini`/`pool`; `spendguard doctor` and
  `spendguard lanes --probe` show activation. (Antigravity replaces the individual Gemini CLI login retired
  2026-06-18.)
- **`spendguard.llm_files` — an input-completeness guarantee, the twin of the max_tokens/output guard.**
  `attach_whole(path)` / `attach_many(paths)` read a WHOLE file (by PATH, so a caller cannot pre-truncate),
  stamp a header the model also sees (`sha256`, line + byte count, `COMPLETE`), and reconstruct the source
  byte-for-byte before returning — raising `PartialFileError` (fail closed) or `FileNotFoundError` rather than
  ever emitting a starved prompt. Reachable through the one sanctioned path, `adapters.call(model, prompt,
  files=[…])`, which assembles the prompt through it. Symmetric to the output guarantee (a reply is never read
  as a short answer when it was truncated); the two are cross-referenced in code so they stay discoverable
  together.
- **Input bounded by the model's real context window on BOTH call paths.** The SDK-adapter guard
  (`adapters._input_fits`) now bounds the prompt by `pricing.max_input_tokens` — the model's published input
  window — instead of only the mostly-empty measured char ceiling, matching what `vendor_call.call` already
  enforced. Both paths count the window with the same accurate, image-aware tokenizer the gate uses (was a
  `chars//4` proxy in `vendor_call`), so a large prose document that actually fits is no longer false-refused,
  and an over-window prompt is refused with the real numbers before it bills. INPUT and OUTPUT are now stated as
  INDEPENDENT axes throughout the token-handling code (input↔`max_input_tokens`, output↔`max_output_tokens`;
  neither constrains the other), with a behavioural guard test (same payload refused under a small window and
  accepted under a large one; output budget identical for a tiny vs a huge fitting input) — to end the recurring
  confusion of reading an OUTPUT figure (the 32k floor, a 4k estimate constant) as an INPUT cap. Stale
  `max_tokens=512` / `else 2048` comments that no longer matched the code were corrected.

### Fixed
- **Realtime-oracle hardening** (from a 4-vendor honestreview of the realtime-reconstruction feature, findings
  verified against the code): Anthropic **cache-creation tokens are now priced** — `pricing.cost_or_unpriced` /
  `realtime_cost` / `batch_cost` gained an optional `cache_creation_tok` (billed at `CACHE_WRITE_5M_MULTIPLIER` ×
  input; default 0, so every existing caller is unchanged), and the realtime oracle passes it — they were dropped
  before, undercounting the recorded realtime $. The paged admin-usage fetch now **fails loud at its page cap**
  instead of silently truncating (a truncated slice reads as "less spend"). A malformed usage bucket (no
  `starting_at`) or a segment with no session id is **skipped with a trace**, never KeyError-aborting the whole
  oracle. Guarded by `tests/test_realtime_oracle_hardening.py`.
- **The z.ai GLM Coding Plan lane now appears in `spendguard doctor` / `lanes` / `--probe`.** It was routable
  (executor `zai-coding` / `pool`) but invisible in the activation surface, because `lanes.status()` assumed a
  host CLI and the z.ai lane is key-based (an HTTP endpoint + key, no binary). A lane now declares CLI-vs-key by
  whether it exposes `_bin`; the z.ai lane reports readiness from its plan key (`ZAI_CODING_API_KEY`, or the
  account's `ZAI_API_KEY`) and renders as `🟢 ready (zai key)`. Guarded in `tests/test_lanes.py`.
- The Gemini lane's usage extractor is named `_usage_from_result` (it had collided with `litellm_adapter._usage`),
  the lane-status test now exercises every subscription lane (not just claude-code/codex), and the shared
  lane-protocol method names (`_bin`/`available`/`run_prompt`) were re-adjudicated **agentically** as PROTOCOL in
  the name registry now that a fourth lane implements them (`_bin` moved COLLISION → PROTOCOL).

## [0.10.0] — 2026-08-15

A large release: an exact-Decimal single-ledger cutover, a stable cross-LLM surface, and a 4-LLM self-review
of the whole client that fixed 16 verified-HIGH and ~50 verified-MEDIUM defects — each confirmed against the
code and regression-tested.

### Added
- **`spendguard.ask` — the ONE stable cross-LLM surface.** Ask N models the same prompt and get an HONEST
  `AskResult` (only OK results carry text; a failure can never be read as an answer), with estimate-first budget
  admission (`budget_usd` → `BudgetRefused` before spend) and a caller-chosen panel size (`n=`). CLI:
  `spendguard ask "…" --vendors a:m,b:m --n 2 --json`.
- **`spendguard serve`** — the ask surface over localhost HTTP (`POST /ask`, `GET /health`, `GET /metadata`) for
  any tool/language. Safe by default: localhost-only; a network-exposed bind refuses to start without a token.
- **Dispatch governor** — bounded per-vendor/lane concurrency + optional RPM + cross-process lane co-governance
  (flock slots), wired into the one `vendor_call` chokepoint so fan-outs queue instead of thrashing or 429-storming.
- **Full failure detail on every non-ok `Result`** (for external consumers like honestreview): `http_status`,
  `provider_error` (the provider's own body), `attempts`, `text_head`, serialized under stable names
  (`elapsed_s`, `finish_reason`, …). Two new failure kinds — **`overloaded`** (429/529, transient) and
  **`payload_rejected`** (400/413/4xx, permanent) — split off from `transport_error`, and transient classes
  now **auto-retry with jittered backoff** honoring `Retry-After`, never past the total deadline.
- **Error-aware subscription lanes** for the panel vendors — the API *outcome* decides lane-unsuitable
  (learn a size ceiling, keep the lane) vs lane-down (cool it), instead of one opaque cooldown.
- **Chunked test runner** (`scripts/test/chunked_suite.py`) — the suite runs in chunks with real per-file exit
  codes, so a green result can never be masked behind a pipe.

### Changed
- **Money is now EXACT DECIMAL in a single ledger (`spend_events`, schema v6).** The old `charges` table and
  micro-integer money were retired via a faithful, sum-proven migration; `budget` is a thin facade over
  `spend_events`, and every reader/writer was repointed. (Fixes the rounding-drift class F6/F7.)
- **Anthropic batch cost is cache-aware, end to end.** The reconcile re-price computed cost from input/output
  tokens ALONE, silently dropping `cache_read_input_tokens` + `cache_creation_input_tokens` and undercounting
  cache-heavy spend; the breakdown is now stored and re-priced. The invented batch cache-read rate (three
  independent copies) was replaced with the provider-published rate, in the pricing table, once.
- **Estimates in library code take their output size from measurement** (`expected_output`), never a literal
  `max_tokens` — an unused cap is free and a low one only destroys the answer, so it was never an expected cost.

### Fixed
- **4-LLM self-review → 16 verified-HIGH defects**, e.g.: private `scope="private"` insights were still pushed
  (`and False` dead guard); a SaaS host check matched a *prefix* not the host (`localhost.evil.com`); the
  spend-event writer was fail-OPEN (now dead-letters) and shared a `_conn` across a fork; `schedule` wiped the
  crontab on an ambiguous `crontab -l` failure; a sqlite↔GIL **deadlock** in the money ledger under concurrent
  fan-out; `spendguard migrate` after the cutover would EMPTY the ledger (now refuses).
- **~50 verified-MEDIUM defects** across ~40 files (worked line-by-line off the finding texts): swallowed
  exceptions on spend/trust paths; leaked file/db handles; honest CLI dispatch + exit codes (a failed
  provider-billing fetch is `unknown`, exits non-zero, never a silent `ok`); one bad JSONL line no longer aborts
  a whole batch; semcache atomic upsert + legacy-dup migration + system-aware dedup key; deterministic
  provider routing by longest prefix; deid maps floor entity names to Presidio's; realtime attribution split an
  hour PROPORTIONALLY across active projects instead of winner-take-all; `estimate-divergence` refuses when a
  verdict is UNJUDGED, not just when it's wrong.
- Deleted Codex sessions are pruned from state (stale est-value no longer accrues forever); `brief`'s computed
  quality-bar is actually returned; the fail-closed `REQUIRE` probe covers litellm / google-genai / vertex, not
  just openai/anthropic.

## [0.9.0] — 2026-08-04

### Fixed — the estimator no longer reads `max_tokens` as "expected output"
- `max_tokens` had three readers, each meaning something different by it: the API a hard ceiling, a pipeline a
  truncation risk, **the estimator an expected cost**. That last one was always wrong — you are billed on tokens
  GENERATED, never on the cap — but it stayed invisible while everyone set a cap. Remove the cap (the correct fix
  for a different problem) and the estimate collapsed: `out_tok 0`, so an 800-page job with a true cost of
  **$28.16 estimated at $4.16**. Under-estimating is the sign that walks past a cap unchallenged.
- **`expected_output.py`** decides it from measurement instead: the class's learned **p90** of COMPLETE outputs →
  the caller's `max_tokens` as their own hard bound → the model's published `max_output_tokens` → **UNKNOWN, said
  out loud, never 0**. Wired through all six estimator paths (realtime chat · Responses · Anthropic messages ·
  both batch estimators · the submit gate), each proved by what it RETURNS rather than by what its source says.
- **p90, not p99×1.5.** The cap-sizing recommendation is a worst case whose job is to size a termination bound;
  using it as the expected cost over-states ~4× (measured on a real class: p90 2,419 vs recommend 9,422). Two
  numbers, two jobs — the same separation, one level down.
- **A published ceiling equal to the context window is rejected as unpublished.** 961 of 2,572 upstream entries
  copy `max_input_tokens` into `max_output_tokens`; returning that would assume a 1M-token response and inflate
  every estimate. A field meaning one thing read as another — the same root as base64-as-tokens.
- The estimate line now states the output basis where a cap decision is actually read: `⚠ output: UNKNOWN … this
  is a FLOOR`, or the ceiling named as a worst case.

### Fixed — truncation measurement was a ratchet pointing the wrong way
- `maxtokens()` computed percentiles over ALL outputs including truncated ones. A truncated output was cut AT its
  cap, so it measures the cap, not the work: the more a class truncated, the lower its measured p99 went, and the
  lower the advice went with it. Percentiles now come from COMPLETE outputs only, and the recommendation is
  floored above any cap that already truncated. On a real class this moved the advice from chasing itself
  downward to **9,422**.
- The truncation warning fired **once per truncated call** — 327 identical lines on one real class, which is a
  warning nobody reads. It now announces once per class and again at decade boundaries, carrying the **rate**
  (the actionable number) and the fix.

## [0.8.9] — 2026-08-04

### Fixed — Moonshot batch was under-priced by 17%
- Batch rates fall back to 50% of realtime when upstream publishes none — the OpenAI/Anthropic convention. **Moonshot
  bills 60%** (https://platform.kimi.ai/docs/pricing/batch.md), so every batch-capable Moonshot model was recorded at
  0.5× where the truth is 0.6×. Under-pricing is the dangerous direction for a spend gate. `sync` now applies a
  per-provider batch fraction; only providers with a PUBLISHED multiplier are listed, and an explicitly published
  batch rate still wins over any fraction.

### Added — kimi-k3 is priced, from the vendor's own page
- `$3.00/1M` in (cache-miss), `$15.00/1M` out, `$0.30/1M` cache-hit — from
  https://platform.kimi.ai/docs/pricing/chat-k3.md, recorded with that URL as its stored `_source`. Its batch rates
  are set to STANDARD, not discounted: Moonshot's Batch API does not support kimi-k3 at all, so a discount there
  would price a mode the model cannot use.

## [0.8.8] — 2026-08-04

### Fixed — an unknown price no longer records as $0
- A model spendguard cannot price (e.g. `kimi-k3`, absent from all 2,391 synced entries) recorded its calls at
  **$0 with a stderr warning**. The warning scrolls away; the ledger then says $0 forever — and $0 is a claim
  that the work was free, which is the one thing we know it wasn't. Such calls are now recorded **UNPRICED**:
  tokens kept, excluded from every total (so nothing reads as free), and named on the receipt with the exact
  command that fixes them. It is the mirror of quarantine — that holds money which cannot be real, this holds
  real usage whose price is unknown.
- **122 upstream entries carried a ZERO token rate, and those were worse**: `price()` SUCCEEDED on them, so
  real spend recorded at $0.00 with no warning at all. `sync` no longer caches an entry whose rates are
  non-positive — a zero is a MISSING price, not a free one — and reports how many it skipped.
- **`spendguard price <model> --in <$/1M> --out <$/1M> --source '<url>'`** supplies a verified rate.
  `--source` is mandatory and stored with the entry: spendguard never invents a price (an invented glm-5.2 stub
  once under-priced a model ~40%), so provenance is the price of entry. Non-positive rates are refused for the
  same reason `sync` now skips them.

## [0.8.7] — 2026-08-04

### Added — realtime output contracts (the batch check, for a lane that cannot be gated)
- `with spendguard.context(intent=..., contract=["patient_id", "findings"])` validates EVERY realtime response
  against the declared shape as it is recorded. A batch can be refused before spending; a realtime loop cannot
  — the money goes call by call — so this reports **early and loudly** instead of gating: the first failure
  prints while the loop is still running, naming the call number and the reason, and the flow receipt reports
  the tally. Opt-in and free when unused; a contract that raises can never break the caller's loop.

### Added — realtime is now cross-checked BOTH ways, with NO admin key
- The comparator is the gate's **own call log**, written locally at call time. It is a floor, not a bill, but it
  proves the thing nothing was checking: that the ledger does not claim MORE than the gate ever observed
  (invented money), and that logged calls are not vanishing before the ledger (dropped recording). Admin keys
  stay a DEV-only cross-check — real use must never need one, and this path never touches them.
- Its first run compared a month of ledger against an all-time log and reported $226 missing that was never
  missing. The window is now required to match, and the docstring says why: an alarm that cries wolf is worse
  than no alarm, because the next real one gets ignored.

### Fixed — a spread artifact is no longer labelled as a finding
- `reconcile-ledger`'s per-day rows called days "over-covered"/"under-covered" when reconcile had merely spread
  backfill across provider-usage days rather than the days the gate recorded on. Days carrying backfill are now
  named not-comparable, and only stand-alone days get a verdict. The NET remains the number that means
  something.

### Fixed
- A flow's contract tally no longer leaks past its own `with` block (nested flows keep their own).

## [0.8.6] — 2026-08-04

### Fixed — lane parity (the asymmetry the last release introduced)
- The impossibility rail shipped in 0.8.4 covered the two BATCH estimators only. **Realtime now carries it too**
  (one request, so the bound is `in_tok` vs the context window). This matters because realtime records from
  actual usage when the SDK returns it and falls back to the ESTIMATE when it does not — the same path that
  produced the invented $54.51 — and the rail also catches a misread `usage` field, which no estimator fix
  would.
- **Remote compute had no plausibility rail at all.** `gpu_port` now rejects derived rate rows that cannot
  describe real usage: negative durations, more than 24h attributed to one instance-day, and start timestamps
  in the future (the seconds-vs-milliseconds mistake). Provider-BILLED rows are never judged — their number
  outranks any derivation of ours.
- **Stream accounting failed SILENTLY.** The stream done-handler swallowed every exception, so a broken
  recorder dropped a call's realtime spend with nothing said. Still fail-open — a recorder must not break the
  user's stream — but now loud about exactly what was not recorded.

### Added — basis labels: every number says what KIND of number it is
- `charges.basis` ∈ `estimate · billed · assumed · reconstructed`, stamped at write time by the writer (who
  knows) rather than inferred by the reader (who cannot). Batch submits are `estimate` — a max_tokens ceiling
  until true-down; realtime is `billed` when the provider returned usage and `estimate` when we fell back;
  reconciliation rows are `billed`. Rows written before the column read as **unlabelled**, never quietly as
  billed. The receipt shows the breakdown under the total: *"basis of the Actual $: billed $X · estimate
  (ceiling until reconciled) $Y · unlabelled $Z"*.

### Added — a behavioural matrix over every cost aggregator
- `tests/test_ledger_marker_matrix.py` pins WHICH rows each of the eleven aggregators counts — quarantined,
  reconciled, true-down, meta — by seeding distinct power-of-two amounts and decomposing each total. Not by
  grepping for a marker name, which a query can mention without using. The right answer genuinely differs per
  reader (the push payload needs backfill; `gate_batch_cells` must exclude true-down or it nets twice), so the
  matrix is the reviewed answer and the arithmetic enforces it. New aggregators fail the test until declared.
- It immediately found a real bug in the repair tool from 0.8.5: `charges.ts` has SECOND granularity and up to
  six charges share one second, so `quarantine --ts` could have tagged five innocent rows. It now targets by
  **rowid** and REFUSES an ambiguous timestamp instead of guessing.

## [0.8.5] — 2026-08-04

### Added — the test that authorizes a bulk run now has to PROVE something
- `test_job`'s verifier was optional, and `verify_fn=None` recorded `verified=1` ("None → trust that it ran").
  A full paid batch could therefore be authorized by a sample that proved only that the API returned something
  — the same DONE-not-CORRECT shape as counting base64 as tokens, and as a leak check that watched one
  direction. **No contract and no verifier is now recorded UNVERIFIED**, with a stderr line saying so. The run
  is still allowed under `warn`/`off` and via `GATE_FORCE=1`; the gate simply stops claiming a verification
  that never happened.
- **`output_contract.py`** — declare the shape once, check it against a real sample:
  required keys · `"json"` · a JSON-Schema-lite dict (`type`/`required`/`properties`/`items`) · any callable.
  **Every item** of the sample is checked, not just the first: the failure that costs money is item 1 parsing
  and item 400 arriving with a sentence before the JSON. Output that only parses after stripping a code fence
  or preamble is counted as **salvaged**, never silently as clean — the downstream parser may not cope, and you
  would find out mid-batch.
- **The test is bound to its contract AND its data.** `gate_ledger` now stores the contract's identity, a
  fingerprint of the sample's inputs, and what the sample actually did (parsed / salvaged / failed / the first
  real failure). Change the contract and the authorization expires ("tested v1, ran v2"); test on three toy rows
  and it will not authorize a run over the real corpus. `check_bulk`'s block message names which of those
  failed and quotes the actual failure.
- Format only, by design: this decides whether output parses into the declared shape. Whether an answer is
  *correct* is a judgement and stays with the agentic quality path.

## [0.8.4] — 2026-08-04

### Fixed — the leak check now watches BOTH directions
- `reconcile-ledger` reported "✓ no material leak" on a ledger claiming **235% of what the provider billed**. It
  only ever measured provider-truth-not-in-the-ledger; money the ledger *invented* had nothing looking at it, which
  is how an impossible $54.51 sat unnoticed for three days. `_compute` now also measures **overhang**
  (accounted − provider), at the same materiality bar, and both the report and the one-line status say so. Some
  overhang is expected — batch estimates are max_tokens ceilings until `true_down` nets them — so the message
  names that first and points at reconcile; what survives a reconcile was never real.
- **Quarantine reached only two of eight cost aggregators.** The first pass fixed `spent_since` and
  `by_provider_day` and missed `by_day` (what the leak check reads — so the leak view still counted the invented
  $54.51) and `by_dims` (the SaaS push payload — so the org dashboard would have received it). All eight now
  exclude it, and a guard test scans the module for any `SUM(cost) FROM charges` query that doesn't, rather than
  trusting a list someone has to maintain.

### Added — the impossible-estimate rail (the other half of the base64 bug)
- Fixing the estimator stopped NEW bad numbers; it did nothing about the one already recorded. A batch went into
  a real ledger at **48,110,544 input tokens for 10 requests** — 4.8M each against a 1M context window — became a
  **$54.51 charge** on a real project, and nothing objected. `reconcile-ledger` stayed quiet too: its leak check
  only ever looked for money that was MISSING, never money that was INVENTED.
- **`gate._implausible_estimate`** now rejects that class outright. A request larger than the model's published
  context window is refused by the provider, so an estimate implying one describes a broken estimator, not a
  batch — a physical bound, not a tuned threshold. When the limit is unknown it says nothing rather than invent
  one. Limits come from the LiteLLM table spendguard already syncs (it is literally
  `model_prices_and_context_window.json`; `sync` now passes the context fields through, same gap `unit_models`
  had) and are read via `pricing.max_input_tokens`.
- A caught estimate is **recorded and QUARANTINED**, never dropped: the row keeps its amount for forensics but is
  excluded from `spent_since`, from `by_provider_day` (so reconcile compares like with like), and from the
  receipt total — where it is **shown as an explicit exclusion** rather than silently vanishing.
- **`spendguard quarantine`** lists batch charges with the per-request arithmetic beside each one, and
  `--ts <ts> --reason <why>` tags a single row. Operator-driven on purpose: the request count behind an old
  batch row is not always recoverable, and a repair that guessed the denominator would repeat the bug it is
  repairing. The change is written to `spend_audit` with its before/after.

## [0.8.3] — 2026-08-04

### Fixed
- **The pre-spend estimate counted base64 image bytes as tokens — every vision batch was refused ~25–50× too
  early.** Each estimator `json.dumps()`'d a message's content and counted the result as text, so a vision
  request was charged for its ENCODED PAYLOAD instead of its pixels. Measured against real Anthropic billing:
  200 448×448 panels estimated 10,174,860 input tokens against **234,300 billed** (26×); a 100-image filmstrip
  batch was 23× over. An 800-page chunk estimated **$71.40** where the true cost is ~$2.17. A cap compared
  against that number blocks work that costs pennies, which is how a gate stops being used.
  New `content_tokens.py` counts what providers actually charge for — pixels, not bytes: Anthropic
  `(w×h)/750` after the ≤1568px downscale, OpenAI's base+tiles after its rescale (with `detail: low` and the
  published gpt-4o-mini multiplier). Dimensions come from the image HEADER — a ~96-byte decode regardless of
  file size — so a 12 MB image costs the same to measure as a 12 KB one. PDFs are counted by PAGE, not by
  byte. When pixels are genuinely unknowable (a remote URL we won't fetch) it falls back to a documented flat
  per-image estimate and says so; it never falls back to measuring the payload.
  Wired into **every** estimator — realtime chat, the Responses API, OpenAI batch `.jsonl`, Anthropic batch
  requests, and the standalone submit gate — so no path is left counting bytes. `estimate_jsonl_cost` now also
  reports `media` / `media_units`.
- **The receipt reported LAST month's plan value under the heading "spend this month."** `stamp_est_value` froze
  today/week/month at stamp time, so a receipt rendered weeks later replayed the stale window as current — on this
  machine, $8,935 (June) where July was $6,758. The cache now keeps 40 days of per-day detail and the reader
  re-buckets against ITS OWN windows, so the number is right however old the stamp is. A pre-0.8.3 frozen record
  can't be re-bucketed, so it is labelled `⚠ STALE`, with its age and the command that re-stamps that specific
  source (`spendguard cc` / `codex` / `chat` — never an invented one). The marker hangs off the PLAN USAGE row,
  not TOTAL: actual $ is read live from the ledger every render and is not stale.
- `spendguard run -- python job.py` (the line in every quick-start) dies on macOS, which ships `python3` and no
  bare `python` — the first copy-paste a new user makes hit a bare "command not found". It now names the binary
  this host actually has and echoes back the corrected command with their own args. Still a SUGGESTION only: the
  runner never execs a binary the user didn't name (same fail-loud-at-the-pin rule as `config.resolve_cli`).
- CHANGELOG dates for 0.8.0–0.8.2 said 2026-07-16; all three were tagged 2026-07-30. Corrected to the tag dates.

## [0.8.2] — 2026-07-30

### `spendguard sources` — one discovery, three signals, no interrogation
- "How do I get my tool supported?" now has an answer that isn't "wait for us": a **transcript-source PORT**
  (`sources.register`, same shape `gpu_port` uses for RunPod/Modal/Lambda). A source declares `NAME` / `detect()`
  / `read()`, registers from a `spendguard.providers` entry point, and appears in **both** `sources` and `scan`
  with zero changes on our side. A broken source warns once and is skipped — never fatal. Documented as Level 4
  in docs/PROVIDERS.md; `scan` now goes through the port instead of hardcoding two readers.
- **`spendguard sources`** answers "where can this machine spend?" from three signals it can see without asking:
  providers with a resolvable key (split LLM vs remote compute — both real $, different caps and reconcilers),
  agent tools on disk, and interpreters with an LLM SDK installed (gated or not, reusing `coverage.audit`).
  Local, free, no LLM, ~1.4s. **It never reads your source code** — checking installed packages and known session
  dirs answers better than grepping repos for `import openai`, and is far less invasive. Keys are reported as
  present/absent; the value never appears in output or JSON.
- `scan`'s empty state no longer dead-ends: a machine with no agent transcripts now sees its providers and its
  ungated venvs, with the next command, instead of "nothing to scan yet".
  Guard: `tests/test_sources_port.py` (24 checks incl. a third-party source appearing end-to-end, a broken source
  being skipped, and the no-source-code / no-network / no-key-leak boundaries).

## [0.8.1] — 2026-07-30

### Fixed — three deferred findings, all of them "the number changes depending on how you ask"
- **UTC/local month-boundary drift.** Every ledger day-key is written in UTC, but all **15** default `since`
  windows were built from `date.today()` — LOCAL. West of UTC that makes the month boundary wrong for 7-8 hours
  around the 1st, so `trust`, `close` and the leak check computed a residual that **changed with the time of day
  and then silently self-corrected** — the hardest possible bug to chase in an accounting tool. One helper
  (`config.month_start_utc()` / `today_utc()`), used by all 7 modules; a guard fails if any module ever derives a
  money window from local time again.
- **Unpriced units were silently $0 in the PRE-SPEND path.** `_est_usd_images` / `_est_usd_speech` returned $0 on
  an unknown model with no warning, while their `_act_*` twins warned. That estimate feeds the CAP check — so an
  unpriced image/TTS model could never trip a cap, and the user was told only *after* the money was gone. Exactly
  backwards for a pre-spend gate. Both now warn (deduped per model), still returning $0 rather than a guess.
- **`--help` / `--version` didn't exist.** The 9-line module docstring — 10 of 60+ commands — printed for every
  help request, every version request and every typo, always exiting 1. Now: grouped help by task (start here ·
  see the money · spend less · teams · setup), `--version`, exit 0 for explicit help, exit 2 + did-you-mean for a
  typo. A guard asserts every advertised command really dispatches, so help can't drift from the code.
  Guard: `tests/test_deferred_fixes.py` (28 checks).

## [0.8.0] — 2026-07-30

### Fixed — plugin discovery WARNed on every import under Python 3.9 (our own minimum)
- `entry_points(group=…)` is 3.10+; on 3.9 it raises TypeError, so `[spendguard] WARN provider-plugin discovery
  failed` printed on **every import** for anyone on the minimum supported version. Now falls back to the 3.9
  dict API. Found by the new "a fresh import is silent" assertion, not by reading the code — which is the whole
  argument for that assertion existing.

### Import-name shadowing is reported in `doctor` (quietly), and a stale artifact removed
- `src/spendguard.egg-info` — a leftover from the v0.2.0 dist rename — was still claiming the `spendguard` import
  name in the source tree, and every interpreter that loads this repo's `src/` saw it. Deleted (it is a build
  artifact and already gitignored). That is what the check found on its very first run.
- `spendguard.which_package()` / `shadowing_dists()` report which distributions provide the import name;
  `spendguard doctor` shows a line only when something other than llm-spendguard claims it. **Deliberately silent
  at import** — the first version printed an ambient stderr warning on every interpreter start, which fired during
  unrelated work in another repo for what was a stale build artifact. Diagnostics belong in the diagnostic command.
- README documents the `chat` extra properly instead of leaving it to be guessed at: opt-in twice, exactly which
  cookie entries it reads, that the AES key comes from your own Keychain (which prompts you and which spendguard
  cannot bypass), that the token is never logged/printed/pushed, the 0600 cache + TTL, and how to skip it.
  Naming stays as decided: dist `llm-spendguard`, import + CLI `spendguard`, domain llmspendguard.com.
  Guard: `tests/test_import_name_shadow.py` (13 checks, incl. "a fresh import is silent").

### `spendguard scan` — a first run that costs nothing and takes 10 seconds
- The front door was `pip install` → `install-hook` → `doctor` → edit your script → run it gated: four steps and a
  venv mutation before a single number. And `report`, the obvious first command, does a live provider-billing pull
  measured at **over three minutes** on a keyless install — a first impression that hangs is worse than none.
- `spendguard scan` reads the Claude Code / Codex transcripts already on disk and prints what that work costs at
  API rates. **No key, no network, no LLM call, no writes outside SPENDGUARD_HOME** — safe to run via
  `uvx --from llm-spendguard spendguard scan` on a machine you don't own. Measured 8.7s cold on 970 sessions.
  It presents the EXISTING readers rather than re-parsing transcripts, and keeps the two axes separate: est plan
  value is labelled as such, shown beside a $0.00 billed line, and never summed.
  Guard: `tests/test_scan_firstrun.py` (21 checks incl. no-network/no-LLM assertions on the module source).
- `ACCURACY.md`: the error rate against provider truth, with a reproducible worked example (batch exact; realtime
  +4.4% over two months) and an explicit **what we do NOT capture** list. Nobody else in this category publishes
  one — the best of them tell you to check an invoice by hand.
- README front door rewritten: `uvx` scan first, the four things nobody else does, a **"what leaves your machine"**
  table (local-only default; LLM attribution and team sync both opt-in), and the stale
  `pip install llm-spendguard # once published to PyPI` line finally removed — it had been on PyPI for 12 releases.
  GitHub description + topics set (the repo card was blank).

### README split: 50K → 13K, nothing dropped
- The README was 49,879 chars / 44 headings — a stranger decides in under 30 seconds, and the one genuinely novel
  capability sat below heading 30. The CLI reference moved to **docs/CLI.md** and the knob-by-knob configuration +
  subsystem prose to **docs/REFERENCE.md** (both in the docs-site nav); the README keeps the pain, the first
  command, the four differentiators, what-leaves-your-machine, install, safety and a "where to go next" table.
- Three claims corrected while moving them: the gate is no longer described as auto-installing via
  `sitecustomize.py` (that's the opt-in now — the wrapper is the default), the team dashboard is **live** rather
  than "in development", and a `#configuration` anchor that no longer resolved now points into the reference.

### `spendguard run -- <cmd>` is the new default way to gate (startup hooks are no longer step one)
- `install-hook` writes `sitecustomize`/`usercustomize` into a venv. On **2026-03-24 litellm 1.82.8 shipped a
  malicious `litellm_init.pth`** that ran a credential stealer at every interpreter start; startup-hook abuse is
  now MITRE T1546.018, endpoint tools ship detections for site-customize file creation, and PEP 648 (which would
  have blessed a sanctioned version) was rejected. A cost tool in the same category as the compromised package
  should not ask strangers to let it write startup hooks as step one.
- `spendguard run -- python job.py` does what `ddtrace-run` / `opentelemetry-instrument` do: a generated bootstrap
  dir on the CHILD's PYTHONPATH, then exec. Nothing is written into site-packages, nothing persists after the
  process exits, the effect is scoped to that one command, and not using the wrapper is the complete uninstall.
  It CHAINS to a host's own sitecustomize instead of shadowing it, is fail-open, and `spendguard run --show`
  prints the exact bytes that will execute (generated locally — never downloaded). `install-hook` remains
  supported and is still the most complete option for a machine you own; it is now the documented opt-in.
- A missing prerequisite is now ONE clean line instead of a traceback: `cli.main` wraps the dispatch and catches
  RuntimeError (the `KeyMissing` base) — `spendguard report` on a fresh install used to dump 14 lines ending in
  `KeyMissing` while the reconcile branch handled the identical condition properly. Also handles broken pipes
  (`spendguard report | head`) and Ctrl-C.
  Guard: `tests/test_runner_wrapper.py` (24 checks incl. an end-to-end proof that a child is armed before its own
  code runs, only under the wrapper, and that a host sitecustomize still executes).

### The gate's cost warning is a budget signal again (reported ~27× over)
- A batch that billed $0.60 warned at ~$16. The RATE was right (batch_cost); the OUTPUT assumption was not — both
  estimators sum each request's `max_tokens` as if every request runs to the limit, while real fill is ~40-55%.
  The gate now attaches the LEARNED expectation (`calibrate`'s already-measured fill/opi quantiles) and every
  human-facing line leads with it: `~$0.60 likely · $1.10 p90 (learned from 1,666 obs @model) · ceiling $16.20`.
  The CAP still compares the ceiling on purpose — a cap must bound what COULD be spent — and the over-cap prompt
  now says "could reach $X if every request runs to its max_tokens (likely ~$Y)". No calibration → the ceiling is
  shown and NAMED a ceiling; a fabricated "likely" is never invented, and a failing learner never reaches the gate.

### Key-missing errors name keys.env, not the legacy .env
- Four user-facing messages (gate doctor, both reconcilers, the install-hook tail) told users to add keys to
  `~/.spendguard/.env` — the LEGACY path, still read for back-compat but created by nothing — while `init`
  scaffolds `keys.env`. Users were sent to write a file the tool doesn't make. All four now print the resolved
  `config.KEYS_ENV`, and a guard fails if any source file ever prints legacy-.env instructions again.
  Guard: `tests/test_estimate_signal.py` (21 checks).

### `spendguard config set` works (it was a documented NO-OP) + honest subscription line
- `config set <section.key> <value>` was documented in four places — including step 5 "Set caps that matter" of
  the docs-site quickstart — and did nothing: `cmd_config` never read argv, so it printed the config table and
  exited 0. A new user "set" three caps on a caps tool and set none. Now registry-driven: writes the store each
  setting's schema names, coerces + validates by `kind`, `null` unsets (back to the default), did-you-mean on a
  typo, refuses secrets (→ keys.env) and knobs another file owns, and warns when a live env var still overrides.
- The receipt's two-axis table DROPPED the `subscription_assumed` flag: an ASSUMED $400 default was printed as
  fact in the column headed **Actual $**, and hardcoded "Subscription (Max + Pro)" even for someone on a single
  $20 plan. Now the row + TOTAL carry `*` with a footnote naming the exact fix command, and the label is built
  from the RESOLVED plans. New registered knobs `subscription.plan_usd` / `subscription.plans` — the footnote's
  fix command previously pointed at a setting `config set` couldn't set.
  Guard: `tests/test_config_set.py` (24 checks, sweeping all 59 registered knobs).

### Moonshot / Kimi is a first-class provider — and vendor-hosted ids finally price
- **Kimi**: `MOONSHOT_API_KEY` + `https://api.moonshot.ai/v1` (OpenAI-compatible). Prefixes cover the whole
  family (`kimi*`, `moonshot-*`), so kimi-k2.5 / k2.6 / kimi-latest — and any future Kimi id — route and
  price themselves the day the synced table carries them: no code change, no hardcoded rate. Mainland-China
  accounts override the base_url with `register_provider()`.
- **The bug that made "breadth" a lie**: the synced LiteLLM table keys most non-first-party models as
  `vendor/model` (`moonshot/kimi-k2.5`, `zai/glm-4.6`) while callers pass the bare id their SDK takes — so
  `price()` raised "no canonical price" for EVERY GLM and Kimi id with a real published rate sitting in the
  cache. `price(model, provider=None)` now resolves bare ids against vendor-qualified keys (raw first, so
  `kimi-latest` isn't eaten by the `-latest` alias strip), accepts `provider:model` / `provider=` to pin a
  vendor exactly, ignores deep reseller paths (`bedrock/<region>/…`, `cloudflare/@cf/…` are a different
  vendor's resale rate), and RAISES on vendors that disagree on price rather than picking one.
- **Removed a fabricated price**: prices.json shipped a hand-typed `glm-5.2` STUB (0.6/2.2) that overrode the
  live layer and under-priced the real z.ai 5-series (glm-5 = 1.0/3.2) by ~40% — a guessed number beating
  real data in an accounting tool. The zai block is now empty by policy; unpriced ids fail LOUD.
  Guard: `tests/test_pricing_vendor_ids.py` (30 checks).

### Lane activation text warns about the API-key trap
- The claude CLI's onboarding offers to use a detected ANTHROPIC_API_KEY — choosing Yes silently meters
  every Claude Code call to the API instead of the plan (hit live during activation). The init/doctor/
  `lanes` activation instructions now say to choose No and sign in with the subscription account, and
  note that login links are one-time CLI-generated (no static URL exists to print).

## [0.7.2] — 2026-07-16

### Lane activation is now PROMPTED, not discovered (`spendguard lanes [--probe]`)
- A plan lane that isn't installed/logged in degrades silently to the metered API at call time (by
  design — never break) — which meant a user could set `advisor.executor = pool` and never learn why
  their plans weren't carrying the work. Now `spendguard init` (tail) and `spendguard doctor` print a
  per-lane activation block whenever the executor covers a lane: CLI found or the install step, login
  verified or the exact command (`claude` → `/login` / `codex` sign-in), plus the consequence line
  ("until active, prompts fall back to the metered API — billed"). `spendguard lanes --probe` verifies
  end-to-end with one tiny plan-billed prompt per enabled lane ($0).
- Auth detection is artifact-based and honest about its limits: a macOS keychain item alone reads
  🟡 unverified, never 🟢 — it can belong to the desktop app while the CLI is logged out (found live).
  Only the CLI's own credentials file (or the probe) proves a lane. Guard: `tests/test_lanes.py`.

## [0.7.1] — 2026-07-16

### Fixed
- `spendguard.__version__` reported "0.3.0" — a hardcoded literal never bumped for four releases. It now
  reads the installed package metadata (single source: pyproject.toml; source-tree fallback 0.0.0.dev0).
  Guard: `tests/test_version_dunder.py` fails on any future drift.

## [0.7.0] — 2026-07-16

### N subscriptions at once: Codex lane + executor pool (`advisor.executor = codex | pool`)
- New `codex_exec` lane runs OPENAI-model meta prompts headlessly on the ChatGPT plan (`codex exec
  --json --output-last-message`), mirroring the claude-code lane's guarantees: OPENAI_API_KEY stripped
  from the child (a plan call can never silently become metered), $0 on the billed axis
  (kind='subscription', executor 'codex'), plan value on the est-value axis via the codex pipeline,
  and {error} → fallback on ANY mismatch. Usage parses by field name from the event stream (tolerant
  of CLI schema drift; absent usage = 0 tokens, never a guess). VERIFIED LIVE: a pool call answered on
  the ChatGPT plan at $0 with real usage captured. Note the CLI's own harness adds ~13K input tokens
  per call — plan tokens, fine at meta volume.
- CLI resolution for daemons (`config.resolve_cli`): launchd/cron run with a minimal PATH that misses
  nvm/~/.local installs, so the lanes resolve their CLIs via $SPENDGUARD_CLAUDE_BIN/$SPENDGUARD_CODEX_BIN
  pin → PATH → well-known user-local dirs (newest executable wins). An explicit pin that doesn't exist
  fails LOUD — never a silent substitute. (The desktop app's embedded claude-code-vm binary is a Linux
  VM executable, not host-runnable — only real host installs count; the claude lane needs a one-time
  `claude /login` on a fresh CLI install.)
- `pool` enables BOTH plan lanes at once, provider-respecting by design: anthropic-model prompts ride
  the Anthropic plan, openai-model prompts ride the ChatGPT plan, never cross-provider substitution —
  the recorded model is always the model that answered. A lane failure (window exhausted, CLI missing)
  cools that lane for `advisor.pool_cooldown_s` (default 900s) so bursts go straight to the API.
  Guards: `tests/test_codex_exec.py`, `tests/test_executor_pool.py`.

### Per-repo keys: profiles, and per-key spend attribution
- KEY PROFILES: one global `keys.env` holds every workspace/project-scoped key as `<VAR>__<profile>`
  entries; a repo's `.spendguard.json` `key_profile` (or $SPENDGUARD_KEY_PROFILE) selects them.
  Precedence: real environment → profile entry → unsuffixed entry; suffixed entries never leak without
  their profile. Pairs with provider-side scoping (OpenAI project keys / Anthropic workspace keys) so
  the provider's own billing splits per repo and reconcile can cross-check each repo against its own
  workspace truth. (`config.load_key_files` now runs at the END of the config module so profile
  resolution can read the repo config — importers still get keys before any client is constructed.)
- KEY FINGERPRINT: every charge is stamped with the serving key's `sha256[:8]:last4` (env-resolved
  proxy, documented; LOCAL-ONLY — the roll-up push never selects it). `budget.by_key()` +
  `spendguard keys` show $ and calls per (provider, key); reconcile/true-down marker rows carry no
  key. Guard: `tests/test_key_profiles.py`.

### Subscription executor honors the chosen model tier (plan-window smartness)
- `advisor.executor = claude-code` used to run every meta prompt on the CLI's DEFAULT model (the top
  tier), silently upgrading haiku-class classify/judge prompts and burning the scarcest plan window.
  The executor now maps the requested API model to the matching `--model haiku|sonnet|opus` family
  alias — the advisor's cheapest-adequate-tier choice holds on the plan exactly as it does on the API.
  Unknown family → CLI default (degrade, never error). Guard: tier checks in
  `tests/test_subscription_exec.py`.

### Prices keep themselves fresh (`pricing.refresh_days`) + per-UNIT rates now flow from LiteLLM
- `sync.refresh_if_stale()` runs at the top of every `saas sync` (which the installed `spendguard
  schedule` agent already runs on a cadence): re-fetches the LiteLLM price cache only when it is older
  than `pricing.refresh_days` (default 1; env `SPENDGUARD_PRICES_REFRESH_DAYS`; 0 = manual only) — an
  hourly agent still refreshes at most once a day. Strictly fail-open (a failed fetch keeps the existing
  cache + curated prices.json) and reloads the in-process table so the same run already prices with
  fresh rates. No dedicated price scheduler — it rides the sync, like the true-down rides the reconcile.
- Fixed the missing pipe for unit billing: `pricing._load_units` reads a `unit_models` section the sync
  never wrote (unit-billed entries have no token rate and were dropped entirely). `sync-prices` now
  passes through per-unit cost fields ($/second, $/character, $/image — 353 models), so transcription /
  TTS / flat-rate image capture price themselves; curated `unit_prices` still win, and truly unpriced
  units still fail loud, never guessed. Guard: `tests/test_price_refresh.py` (16 checks).

### Estimate→actual TRUE-DOWN at reconcile (`ledger_sync.true_down`, rides the daily cadence)
- The gate records a batch's cost at SUBMIT time — an estimate (the batch id doesn't exist yet). The
  provider later bills the actuals per batch. Reconcile now nets the two: per (provider, model), the
  over-estimate Δ = estimates − billed (from the per-batch reconcile caches, both providers) is written
  as NEGATIVE correction rows spread proportionally across the estimate cells (project × day). Original
  estimate rows are NEVER mutated (forensic: the ledger keeps what we thought AND what it billed);
  corrections carry the REAL model + a `(true-down)` conv_id sentinel, so `by_dims` NETS them per
  dimension before any SaaS push (the server clamps negative cost — netted rows never trip it).
  Idempotent per window (cleared + rebuilt from current billed truth each run): a re-run is a no-op and
  an in-flight batch that trues down today self-heals when its actuals land. A provider whose billed
  fetch FAILED is skipped — unknown must never read as $0 billed. Under-estimates stay the gap
  machinery's job (two one-way valves meeting at billed truth). Runs FIRST inside
  `reconcile_into_ledger`, so the gap/attribution math sees corrected numbers; summary gains a
  `true_down` block. No new scheduler — it rides the existing daily reconcile.
- Trust check is now APPLES-TO-APPLES, axis by axis (`trust._ledger_llm_total`): batch = gate estimates
  netted with true-downs ↔ provider-billed batch; realtime = gate-live rows ↔ the gate's own realtime
  log. `budget.by_day(exclude_reconciled=True)` now excludes ALL reconcile mirror markers (provider-batch
  + realtime history/oracle/reconstructed), killing the phantom drift where mirror rows inflated only the
  recorded side. Fixes the standing ALARM (recorded 1.40× billed) that fail-closed blocked `saas sync`.
- Guard: `tests/test_true_down.py` (24 checks — netting, proportional attribution, idempotence,
  in-flight self-heal, failed-fetch skip, marker drift, trust verdict flip, full reconcile integration).

## [0.6.0] — 2026-07-14

### Autotune: learned max_tokens applied at call time (`gate.autotune = off|suggest|apply`)
- What `spendguard maxtokens` measures becomes a default you can't forget: at call time the gate
  compares the caller's `max_tokens` with the call-class's OBSERVED output distribution. `suggest`
  (default) prints the delta once per class; `apply` SHRINKS a wasteful cap to the measured p99×1.5 —
  never raises a cap, never adds one, vetoed under 30 observations or by ANY truncation history (one
  truncation permanently backs the class off — the recorded truncation counter IS the backoff state),
  per-call opt-out `autotune=False`, every application logged. No counterfactual "saving" is recorded:
  the value is accurate estimates + runaway-output protection. Guard: `tests/test_gate_autotune.py` (12).

### Subscription executor (`advisor.executor = api|claude-code`)
- spendguard's OWN meta prompts (insight synthesis, auto-fresh, judging) can ride the flat-fee plan:
  a one-shot headless `claude -p --output-format json --max-turns 1` (no agent loop, no tools; the
  provider key env var is stripped from the child so a plan call can never silently become metered).
  Recorded at $0 on the BILLED axis (kind=`subscription`); plan value lands on the est-value axis via
  the existing claude-code pipeline. Any failure falls back to the caged API path — degrade, never
  break. Guard: `tests/test_subscription_exec.py` (12).

### Run-rate month-end forecast in `spendguard close`
- Open month with ≥5 observed days: "month-end ~$X (p50) … $Y (p90)" = MTD + remaining days × the
  month's own daily median/p90 — labeled an extrapolation, never shown for closed months or thin data.
  (The org statement on the server gained the same line.) Guard: forecast block in `test_auto_fresh.py`.

## [0.5.0] — 2026-07-13

### Every remaining spend channel captured (ft / units / tool fees / raw HTTP / Gemini embeddings)
- **Fine-tuned models priced correctly**: `ft:BASE:org::job` resolves to the table's `ft:BASE` entry (or a
  dated LiteLLM-layer variant); an unpriced ft id fails LOUDLY — the base price is never a substitute
  (ft inference bills above base). Guard: test_pricing ft block (5).
- **Gemini embeddings**: `google.genai embed_content` (sync+async) joins the vertex capture — per-embedding
  `statistics.token_count`, provider='google', fail-open. Guard: `tests/test_vertex_embed.py` (7).
- **Non-token surfaces**: images.generate, audio.transcriptions (token-billing 4o-transcribe AND
  per-second whisper), audio.speech (per-character), fine_tuning.jobs.create (recorded as a LOUD
  unestimated submission — its $ lands at reconcile) — all budget-enforced via a new $-direct precheck.
  Unit prices come from a new `pricing.unit_price()` (curated `unit_prices` + LiteLLM per-unit fields);
  the SHIPPED table carries no invented numbers — unpriced units record at $0 with a per-model warn.
  Guard: `tests/test_gate_units.py` (15).
- **Per-call tool fees**: web-search invocations (Responses `web_search_call` items, Anthropic
  `server_tool_use.web_search_requests`) are counted and recorded as their OWN fee row — token usage
  never contains them. Vector-store/file storage stays reconcile-absorbed (day-level), documented.
- **Raw-HTTP capture** (`http_capture.py`): httpx/requests calls straight at provider hosts are parsed
  for usage (chat/messages/embeddings shapes) into the same realtime ledger; unparseable provider
  responses log a LOUD `raw_http_unmetered` event. SDK-originated traffic is suppressed via a
  ContextVar around every gated call — no double count. Capture-first: never blocks, never alters a
  request. Knob `SPENDGUARD_HTTP_CAPTURE=off`. Guard: `tests/test_http_capture_toolfees.py` (14).

### GPU-provider port + RunPod / Modal / Lambda adapters (remote compute beyond vast.ai)
- **`gpu_port.py`** — the explicit port every GPU provider implements (`GPUProvider`: `configured()`,
  normalized `instances()`), with the per-UTC-day dph×hours splitting math EXTRACTED from the vast.ai
  implementation into one shared helper (`day_slices` / `cost_by_day`); `resources.py` now calls it, behavior
  identical (its tests pass unchanged, plus an explicit equivalence check). Includes the GPU-source REGISTRY
  `reconcile.all_sources` iterates — vast.ai (`"gpu"`) and every adapter ride `spendguard reconcile all`
  through the same loop, and third-party plugins join via `gpu_port.register_source` from their existing
  `spendguard.providers` entry-point `activate()`.
- **Adapters against DOCUMENTED provider APIs** — RunPod (`RUNPOD_API_KEY`, GraphQL `myself{pods}`, RunPod's
  own `costPerHr`), Modal (`MODAL_TOKEN_ID`+`MODAL_TOKEN_SECRET`, the documented `modal.billing`
  workspace report — per-app per-day BILLED $, which is also the account truth), Lambda (`LAMBDA_API_KEY`,
  `GET /api/v1/instances`, Lambda's own `price_cents_per_hour`). Never a hardcoded $/hr table.
- **Honesty over coverage** — an unconfigured provider is silently skipped (never an error, never fake data);
  a row the API doesn't price is `{"unpriced": true}`; a runtime the API doesn't expose (Lambda's listing has
  no launch timestamp; RunPod's stopped pods) is `{"untimed": true}` — visible UNKNOWN, never $0-clean; a
  provider with no billing endpoint reconciles with truth `unknown`, never "covered". Attribution mirrors
  vast: instance label → project via config `resources.<provider>.label_map` (empty default — no guessing).
  Guard: `tests/test_gpu_port.py` (55 checks, offline against documented payload shapes — fixture doc-URLs
  cited inline; NOT live-verified); the runner now also strips the new provider keys. Recipe:
  `docs/PROVIDERS.md` §GPU.

### Embeddings: fully captured (two blind spots closed)
- **Realtime `client.embeddings.create` is now intercepted** (sync + async): estimated from the input
  (strings, lists, or pre-tokenized id arrays), checked against the realtime budget like any other call,
  accounted at the table price (out=0), and recorded to the corpus. Previously invisible: not patched,
  not recorded, and not provider-reconcilable without an admin key.
- **Batch JSONL bodies carrying `input`** (embeddings / Responses-style) are now estimated — they used
  to count $0 input, so the pre-spend cap could never see an embeddings batch coming (actuals were
  already trued up at reconcile; now the GATE sees them too, priced at the batch rate).
  Guard: `tests/test_gate_embeddings.py` (12).

## [0.4.0] — 2026-07-12

### SQLite index audit — every query planned, every hot path indexed, drift impossible
- Audited every extractable SQL statement in the codebase with `EXPLAIN QUERY PLAN` against the full
  schema. Added the missing indexes at each table's creation site (existing installs upgrade on next
  open): `calls(ts)` (as_of/since range reads), `gate_calls(model)` (model-level fill observations),
  `graph_edges(rel)`+`(src)` (rebuild deletes, node joins), `charges(conv_id)` (chat↔charge
  attribution joins), `cost_predictions(paired_ts)` (pair scans). Verified on the live corpus: the
  four former table-scans now run as indexed SEARCHes. Guard: `tests/test_sql_index_audit.py` —
  asserts the required index inventory AND plans ~40 extracted queries, failing on any unindexed scan
  of a growth-prone table unless it is a registered whole-corpus aggregate.

### Fast doctor + suite speed/offline gates (incident #25)
- **`spendguard doctor` is instant**: the leak verdict is read from `leak_line.json`, written as a
  byproduct wherever `leak_line()` already computes (daily report / reconcile / close) and shown WITH
  ITS AGE ("as of 2.1h ago"); no cache = honest "leak status UNKNOWN — run reconcile", never a silent
  skip; `--live` forces the full ~30-day provider pull (previously the default: 3.5 min per doctor).
- **Test suite 23 min → ~25 s** and un-regressable: the runner injects a dead proxy + strips provider
  keys (an accidental live call inside the "offline" suite — the very bug that hid doctor's provider
  pull for weeks — now fails in milliseconds, loudly), runs `pytest -n auto`, and enforces a per-file
  30s wall budget every run (a future hog fails the suite the day it appears). Direct collection of a
  script-style test (`pytest tests/test_x.py`) errors immediately with the canonical command instead
  of hanging. Guards: `tests/test_gate_cli.py` (doctor <2s + cached/UNKNOWN wording), `test_runner.py`
  budget assert, `tests/conftest.py`.

### Learned cost estimator (`spendguard calibrate`)
- **`calibrate.py`** — predicts a planned job's $ from YOUR captured history, correcting the naive
  estimate (input≈len/4 · output=max_tokens · flat realtime price) where it predictably misses:
  models rarely fill max_tokens, tokenizers drift, batch ≠ realtime, caching lands. Learns quantile
  distributions per (activity label, model) — FILL (out÷max_tokens, from the existing `gate_calls`
  truncation telemetry), OUT_PER_IN, $ RESIDUAL vs the pricing table, and IN_RATIO (from paired
  predictions) — with empirical-Bayes shrinkage across an exact→model→global hierarchy; sparse cells
  borrow strength, zero data degrades to the naive answer. Every prediction returns `{p50_usd,
  p90_usd, level, n_obs, basis, naive_usd}` — confidence is part of the answer. Pure sqlite
  statistics: zero LLM spend per estimate; prices only via `pricing.py`.
- **Prediction↔actual loop** — `calibrate.record_estimate(job_id, …)` logs a caller's prediction
  (distinct from `bulkgate.record_estimate`, which authorizes worst-case spend); the gate captures
  the actuals; `calibrate pair` joins them (exact via `calls.context(chain=job_id)`, else
  label+model inside the pairing window; a closed window with no actuals stays visible as expired —
  UNKNOWN never reads as $0). The daily report auto-pairs. Consumers wire in with three calls:
  `estimate(...)` before, `record_estimate(...)` at submit, `calls.context(intent=label,
  chain=job_id)` around the run — no spendguard changes needed per consumer.
- **Ship gate met (backtest on the real corpus)** — `spendguard calibrate backtest` time-splits each
  cell 70/30 and scores held-out median abs % error, naive vs learned, with naive given PERFECT input
  knowledge: overall 18%→10%; worst naive cells corrected 2978%→23%, 5427%→385%, 124%→12%; the one
  regressed cell is printed, not hidden. Surfaced in `spendguard estimate --label` and `advise`.
- **Org-shared learning (`calibrate push|fetch`)** — members share SUFFICIENT STATISTICS only
  (`{n, p50, p90}` per cell; labels de-identified; never prompts/outputs/$) via `POST /v1/calibration`;
  `fetch` caches the n-weighted org aggregate and `estimate()` shrinks toward it: the org's experience
  is the PRIOR, local stats always on top (an exact-label org cell outranks local cross-model pools,
  never local cell evidence). Auto push+fetch+pair ride the daily report; `visibility=private` shares
  nothing. Also fixed in this arc: the `spendguard estimate` CLI dispatch was silently broken
  (`__init__`'s `pricing.estimate` re-export shadowed the submodule) — fixed + regression-guarded.
  Guard: `tests/test_calibrate.py` (32).

## [0.3.1] — 2026-07-06

### Realized efficiency + loss-led guarded framing (#47)
- **`spendguard realized [--sync]`** — MEASURED before/after $ per call around each insight's adoption
  (the corpus that priced the calls is the ruler): realized = Δrate × after_calls, regressions shown, not
  hidden. `--sync` records new positive deltas into the guarded pipeline as **source=`realized`**
  (incremental + idempotent via `realized_state.json`) — the dashboard's "≥ certain" floor now includes
  measured wins, and the panel headline is loss-led ("would have cost ~$X MORE without the guardrails").
  The daily report syncs automatically. Guard: `tests/test_realized.py` (12).
- **Auto-fresh Learnings (#49)** — `advisor.auto_fresh` = `off|weekly|daily` (default weekly): the daily
  report now runs a SMALL caged review (top-3 intents, caps.meta-bounded, estimate-first) when due, so
  Learnings track recent activity without manual `review --run`. State in `review_state.json`; a refresh
  failure never breaks the report. Guard: `tests/test_auto_fresh.py`.
- **`spendguard close --account`** — the account-level reconciliation view for SHARED provider accounts:
  account-wide truth + the machine accounted-vs-provider line, with the explicit caveat that each org's
  statement residual includes its siblings (the honest lens incident #23 pointed at).


## [0.3.0] — 2026-07-06

### Prompt-efficiency loop (`spendguard prompts` + pluggable judges)
- **`spendguard prompts`** — zero-spend lint over the call corpus, per intent (≥5 calls), ranked by
  measured $ at stake: `boilerplate` (a shared prefix ≥60 chars re-sent every call → cache/template it),
  `context_spread` (input p95 ≥ 3× p50 → stuffing), `truncation` (finish=length → max_tokens ≈ p99×1.5),
  `model_mix` (the intent already runs ≥2× cheaper elsewhere → measured cascade candidate). Every finding
  carries its exact next command; prices from pricing.py only. Guard: `tests/test_prompt_lint.py`.
- **Pluggable equivalence judges** — `equivalence.grade` (and `spendguard experiment --semantic`) now
  accepts `custom:<module.fn>`: your own callable `(ref, out) -> 0..1` (wrap promptfoo assertions, schema
  validators, domain checks). The custom score rides the same promote/keep decision as the built-in ladder.
- **The documented loop** — `docs/PROMPT-EFFICIENCY.md`: lint → batch-1 of the same shape → graduated A/B
  (`experiment`, caged + estimate-first) → promote-and-keep with the insight lifecycle re-validating wins.

### Monthly close (`spendguard close`) + truth in the daily sync
- **`spendguard close [--month YYYY-MM] [--csv]`** — the client half of the monthly close: provider-truth
  totals per provider for the month (same numbers `truth --push` syncs), the ledger leak line for the open
  month, CSV export, and a pointer to the org server's full attributed statement (`/statements`: real-$
  classes, projects, teams, and the ledger-vs-truth residual NAMED per provider; est plan value on its own
  axis, never summed). Guard: `tests/test_close.py`.
- **`saas sync` now pushes provider truth automatically** (`out["truth"]`), so a daily-synced org gets
  statement variance with zero extra steps — fail-open, visibility-gated, keys stay local.

### Provider-truth sync (`spendguard truth`)
- **Per-day provider totals → the org server; keys never leave the machine.** `truth.rows()` reuses the
  report's own fetchers (openai/anthropic/vastai) and `spendguard truth --push` sends only {day, provider,
  usd} to `POST /v1/truth` (visibility-gated; a server without the endpoint yet → friendly skip). This is
  the client half of API-based invoice-grade reconciliation — the server's monthly close statement will
  show variance vs these numbers. Guard: `tests/test_truth_sync.py`.

### Daily anomaly detection (the automated gut check)
- **The daily report now z-scores TODAY against each source's own history** (median/MAD — robust to prior
  legit spikes) and prints `ANOMALY` lines (email included) when a day is statistically wild (z≥3.5) AND
  material (≥$5, ≥1.5× median — both real double-count P0s were ~1.8–2× systematic inflation, so a 2× gate
  would have missed them). A synthesized TOTAL series catches a spike hiding in a source too new to judge
  alone. A failed check prints UNKNOWN, never silence. Guard: `tests/test_anomaly.py` (14 checks incl. the
  report wiring). New module: `anomaly.py` — pure, zero new data plumbing.

### Provider plugin API (community-sized provider additions)
- **`pip install spendguard-provider-<x>` is now all a user does.** New `spendguard.providers` entry-point
  group: `spendguard.install()` discovers installed plugin packages and activates each (zero-arg, idempotent
  `activate()`), FAIL-OPEN per plugin — a broken plugin warns once and is skipped, never breaking the gate,
  other plugins, or the user's calls (`provider_plugins.py`). Recipe: `docs/PROVIDERS.md` (3 levels:
  pricing-only / `register_provider` adapter / full `gate.register` interception).
- **Conformance kit** (`spendguard.provider_kit`): third-party provider packages prove themselves in their
  own CI — `assert_conformance(activate, name=..., sample_model=...)` checks registration, pricing via
  `pricing.price()` (never hardcoded), idempotence, and loader fail-open containment. Guard:
  `tests/test_provider_plugin.py`.



### Configuration — two files, placeholder secrets, documented enums
- **`spendguard init` now scaffolds `~/.spendguard/keys.env`** (chmod 600) with a blank placeholder for every
  secret — LLM provider keys, `VAST_API_KEY` (remote compute), and `SPENDGUARD_SAAS_KEY` (the team/org roll-up key).
  The file is **loaded into the environment on `import spendguard`** (`config.load_key_files`), so a user's own
  `openai.OpenAI()` / `anthropic.Anthropic()` calls pick the keys up too — a real env var always wins and blank
  placeholders are skipped (prod / CI / secret-managers are never clobbered). Legacy `~/.spendguard/.env` still honored.
- **`gate.enforce` (the estimate→test→run rail) and `VAST_API_KEY` are now in the config registry** (`config_schema`),
  so `spendguard config` lists them and the enum is documented in one place: `gate.enforce` = `off | warn | block`.
- README **Configuration** section now documents the two files + an enum table (`gate.enforce`, `deid.engine`,
  `saas.visibility`, `saas.sync_interval`, `budget.backend`). Guard: `tests/test_keys_env.py`.

### Providers
- **z.ai / Zhipu GLM** — `glm-*` models route to the new OpenAI-compatible `zai` provider; the key is
  `ZAI_API_KEY` (goes in keys.env, scaffolded automatically). glm-5.2 ships a clearly-flagged **STUB** price in
  `prices.json` — replace it with z.ai's published per-1M rates before relying on its cost numbers.

### De-identification of egress text (privacy)
- **Every text field that leaves this machine now passes through a deterministic de-id floor at the wire.** New
  `spendguard.deid` module: a typed denylist (email, US phone, SSN, credit-card w/ Luhn, IPv4/IPv6, common API-key
  & bearer/JWT shapes, PEM private-key blocks) + the legacy `$`-amount scrub — while generalizable signal (ratios
  like "26x", model names) is KEPT. Wired into **all three** prose egress paths: insight abstracts (`share`), and
  the work-done **commit subjects** and **caged summary** (`saas.push_workdone`) — the latter two were previously
  pushed with only an LLM *instruction* to scrub, never a guarantee.
- **Client-configurable + opt-in NER.** `deid.engine` = `regex` (default, zero-dep floor) · `presidio` (floor +
  Microsoft Presidio for names/locations/dates — `pip install llm-spendguard[deid]`, degrades to the floor and
  warns once if absent, never blocks egress) · `off` (no redaction — a deliberate footgun for trusted data).
  `deid.entities` restricts which types are masked. De-id is a SAFETY/extraction step (regex+NER), not a meaning
  decision — the agentic boundary (project/intent/quality → LLM) is untouched. Fails open toward privacy; never
  raises. Guard: `tests/test_deid.py` (every class masked, signal survives, Presidio-absent fallback, and the
  egress **wiring** — `share._scrub_text` + `push_workdone` commits/summary actually route through deid).

### Central caps (org/team policy → client)
- **The gate now applies org/team spending caps pulled from the dashboard.** `spendguard saas sync` pulls the
  scope's effective caps from `GET /v1/policy` (set per org/team in the dashboard's Caps tab) into config.json
  `policy`. `config.class_cap()` then applies them: an **enforced** cap is a hard ceiling — effective = min(local,
  enforced), applied even with no local cap, and a dev's local config may only *tighten* it, never loosen (the
  Enterprise lock). An **advisory** cap is the org's *suggestion* only — surfaced (via `policy_caps()`) but it never
  changes the effective cap, preserving "partner, not supervisor" for the OSS/Community path. Guard:
  `tests/test_central_caps.py` (enforced ceiling, advisory-is-suggestion, env interplay, pull persistence, fail-open).

### Provider breadth
- **Azure OpenAI — covered for free.** `AzureOpenAI` / `AsyncAzureOpenAI` reuse the same `openai.resources` classes
  the gate patches, so their `.create` IS the gated method — no Azure-specific code. Locked by
  `tests/test_provider_coverage.py` so it can't silently regress.
- **LiteLLM coverage (`spendguard.install_litellm()`).** Captures spend for ANY provider LiteLLM normalizes
  (Bedrock, Vertex/Gemini, Cohere, Mistral, …) via LiteLLM's native success-callback — recorded into the SAME
  realtime ledger as the SDK gate (priced through `pricing.py`), so it rolls up + reconciles identically. SKIPS
  openai/azure (already captured by the SDK gate) to avoid double-counting; fail-open; idempotent. Heavy/optional,
  so the startup gate only auto-wires it if `litellm` is already imported — LiteLLM users add the one-liner after
  `import litellm`. Records LiteLLM's OWN computed cost (`response_cost`) so exotic providers are priced even when
  `prices.json` doesn't carry them.
- **Direct AWS Bedrock (`spendguard.install_bedrock()`).** Patches botocore's dispatch and records bedrock-runtime
  usage — Converse from `response['usage']`, InvokeModel from response headers (no body consumption) — for teams on
  boto3 directly (not via LiteLLM). Capture-focused, strictly fail-open (never alters/blocks the AWS call).
- **Direct Google Gemini / Vertex (`spendguard.install_vertex()`).** Patches google-genai `generate_content`
  (sync + async), recording `usage_metadata`, labelled `provider=google`. Same fail-open contract.
- **Unpriced models degrade gracefully.** `_record_rt` now accepts an explicit cost + provider, and a model missing
  from `prices.json` records its TOKENS at $0 with a visible warn (never a guessed price, never a silent drop) — add
  the sourced rate to `prices.json`, or route through LiteLLM for automatic cross-provider pricing. Guarded by
  `tests/test_provider_coverage.py` (21 checks: Azure-free · LiteLLM record/skip/fail-open · Bedrock · Vertex).

### Security / hardening
- **Gate fail-open hardening + property/fuzz tests.** The gate sits in the call path of every LLM call, so it now
  upholds two invariants under fuzzing (`tests/test_gate_properties.py`, Hypothesis): **passthrough** — it returns
  the underlying call's result unchanged (same object for non-stream; same chunks, in order, for a stream); and
  **fail-open** — only a deliberate enforcement decision (`SpendGateRefused` / `GateBlocked`) may raise into the
  caller, while ANY other internal error (estimator bug, precheck hiccup, accounting failure, stream-proxy error) is
  swallowed and the call proceeds. The realtime wrapper got explicit pre-call (`_rt_precheck_guard`) and post-call
  (`_account_failopen`) guards to match the batch path's `_guard`, and the streaming proxy now guards per-chunk usage
  capture so a usage-parsing bug can never drop a chunk. The fuzzer caught both gaps before they could ship.
- **Signed releases + SBOM.** `release.yml` now publishes to PyPI with **PEP 740 attestations** (Sigstore-backed
  provenance), signs the sdist+wheel with **Sigstore** (keyless, via the GitHub OIDC identity → `*.sigstore.json`
  bundles on the GitHub Release), and attaches a **CycloneDX SBOM** (`sbom.cdx.json`) covering the full dependency
  surface incl. `[all]` extras. Release notes include the `sigstore verify` command.

### Testing
- **Coverage pass on the money-critical core + a scoped CI gate.** New offline tests for `tag.py` (attribution
  cascade, 0→100%), `guard.py` (the guarded-spend lognormal cumulants, 43→100%), `signal.py` (efficiency roll-up,
  0→49%), `pricing.py` (now also `freshness`/`providers`/`_load`/`main`, 54→75%), `reconcile.py` (`all_sources`/
  `report`/base `Source`, 61→92%), and `gate.py` (`realtime_by_day` + the CLI surface, 56→67%). CI now enforces
  **two floors**: a whole-package regression floor (40%) AND a **78% floor on the money-critical core** (gate,
  ledger, reconcile, pricing, attribution, …) — today 81%. The package number is held lower on purpose: I/O-adapter
  modules (chat→claude.ai, saas push, transcript parsers, paid-call tools) are integration-tested, not unit-tested.

### Fixed
- **Est-value buckets by REPO (git-root), not cwd basename — the attribution-quality fix.** Claude Code / Codex
  est-value was keyed by the session's cwd *basename*, so one repo's work fragmented across its subdirs
  (`lmm/scripts/fanout` → `fanout`) and incidental dirs leaked in — `--all` showed ~80 buckets. Now `_project_of`
  resolves the **git-root basename** (cached, via `config.git_root_project`), matching how actual-$ is tagged; a
  non-repo cwd falls back to its basename. Re-bucket existing data with `spendguard cc show --rebuild` /
  `codex show --rebuild` (collapsed ~80 → ~dozen real repos in practice; `lmm` reabsorbed its subdirs).
- **Local receipt is now ORG → TEAM → PROJECT (the attribution model, matches the dashboard).** Est-value is stamped
  as flat cells keyed `org|team|project` from the agentic classifier (`cls[sid]` — the SAME org→team×project the
  server rolls up), and `spendguard receipt` renders the nested tree under a global billed/plan header (`render_tree`
  / `_est_tree` / `_est_tally(org, team, project)`). `--all` = every org, `--org X` = one, default = the connection's
  org (falls back to all if its taxonomy org differs). e.g. `healiom → clinical-ai → concept-model / lmm-port`,
  `ensight → engineering → llm-spendguard / omega`. The status line / Stop hook stay a one-line global tally.
- **OpenAI Codex models priced (parity with the Claude family).** `gpt-5.5-codex` / `gpt-5-codex` now normalize to
  their base GPT's published rates (codex bills at the base model — a verified alias, not a guess), so a Codex
  session on a `-codex` model id no longer `KeyError`s into a silent $0. `price()` tries an exact PRICING entry
  first, so a verified codex/o-series entry can still override. `-latest` is also stripped.

### Added
- **Contextual + proportional receipt (no MCP needed).** `spendguard receipt` now defaults to **this conversation's
  repo(s)** (collapsed, via the ledger's `conv_id` + cwd) and `--all` expands to **every repo, ranked by spend** with
  the long tail summarized. Each repo shows its **proportional plan share** — est-value as a % of total plan usage,
  plus the **$ slice** of the flat plan when a price is set (`subscription.plan_usd` / `SPENDGUARD_PLAN_USD`). The
  in-chat hooks now run with `SPENDGUARD_NO_AUTOINSTALL=1` so the read-only receipt **skips patching the SDKs** —
  **0.6s → ~0.05s** (it never needed the gate). And `spendguard install-rule` now tells the assistant to surface the
  receipt each turn — the desktop/web answer, since statusLine is terminal-only. (We chose NOT to ship an MCP server:
  it adds per-machine install complexity and still can't auto-display every turn off-CLI — net negative here.)
- **Enforce the gate on remote/distributed compute — `spendguard remote {onstart|verify|sync}`** (`remote.py`). The
  gate only governs the interpreter it's loaded in, so a freshly-spun-up vast.ai box's `python3` is UNGATED until
  provisioned. `remote onstart` emits the secret-free boot snippet that installs + hooks spendguard so EVERY python3
  on the box is gated from boot (bake into the instance onstart — covers all scripts, not one). `remote verify --ssh
  '<prefix>'` is a FAIL-CLOSED check (exit≠0 if the box isn't `ENFORCING`, so the orchestrator aborts rather than
  spend ungated). `remote sync --ssh '<prefix>' --project X` pulls the box's realtime ledger and rolls it into the
  local ledger under that project — IDEMPOTENTLY (keyed by `conv_id=remote:<label>`; re-sync replaces, never
  double-counts). Principle: **gate at provision, verify before spend, sync before teardown.**
- **Full OpenAI + Codex parity — accounting works the same for both providers and both coding agents.** New
  `codex.py` (+ `spendguard codex show|classify|sync`) mines `~/.codex/sessions/**` into est-value (channel=codex,
  billed=false — Codex on a ChatGPT/Codex plan is plan-covered, exactly like Claude Code), classified
  org→team×project and **summed into the same receipt/tally** as Claude Code + claude.ai (per-source, never
  clobbering). The token total comes from the cumulative `token_count` events; the model from `turn_context`. The
  gate now also intercepts the OpenAI **Responses API** (`client.responses.create`, sync + async) — previously only
  Chat Completions was gated, so modern OpenAI realtime (incl. Codex-style `responses` calls) was an un-gated
  actual-$ gap; now estimated pre-call + recorded post-call (incl. `input_tokens_details.cached_tokens`) like every
  other surface.
- **Receipts scope to the relevant repo/conversation.** The tally is no longer a global sum — `tally(project, conv)`
  scopes BOTH axes to the current repo (and conversation, via the ledger's `project`/`conv_id` columns + per-project
  est-value buckets). The status line scopes to its session's cwd, the Stop hook + per-flow receipts to the running
  repo; `spendguard receipt --project X` / `--cwd P` scope manually (no arg = global overview). Scope is shown as
  `[project]`. NOTE: `statusLine`/Stop-hook are **terminal-CLI features** — they do not render in the desktop/web
  app; there, use the inline per-flow receipt, `spendguard receipt`, or a file sink.
- **`python -m spendguard …`** (`__main__.py`) — identical to the console script, but works where the script isn't
  on PATH (e.g. gating an ephemeral GPU box: `pip install llm-spendguard && python3 -m spendguard install-hook …`).
- **Configurable receipt surfacing.** `receipts.sinks` / `SPENDGUARD_RECEIPTS_SINK` = `stderr` (default) | `stdout`
  | `file:<path>` (comma-separated) controls WHERE the auto-emitted receipt goes — a **file sink** lets any host
  without an in-chat hook (Codex, an editor, a tmux/menubar widget) display the tally by tailing the log.
  `spendguard install-receipts [--host claude-code|codex] [--remove]` installs/removes the always-on surfacing
  reproducibly (idempotent; backs up `settings.json`) instead of hand-editing it.
- **Inline spend receipts + an always-on tally (`receipt.py`, `spendguard receipt`).** After every gated FLOW
  (a `with spendguard.context(...)` block, a batch submit at the gate, or a CLI command) spendguard emits a compact
  receipt — what ran · in/out tokens · est→actual · the running **today / 7d / month** tally — so what it tracked is
  visible AS IT HAPPENS. The two axes stay SEPARATE and are never summed: **actual-$** (billed, from the gate ledger)
  vs **est-value** (Claude Code + claude.ai plan usage, stamped per-source so they sum, with an as-of date). Per-FLOW,
  never per-call. Verbosity via `receipts.level` / `SPENDGUARD_RECEIPTS` = `off | footer | flow | verbose` (default
  `flow`); auto-emit → stderr (never corrupts piped stdout), `spendguard receipt` → stdout. Zero LLM, no admin key.
  Two Claude Code hook protocols built in: `receipt --statusline` (always-on footer: `cwd · model · ctx% · tally`)
  and `receipt --stop-hook` (a per-turn `systemMessage` line in the transcript).
- **`spendguard schedule [--daily] [--remove]`** (`schedule.py`) — installable cross-platform scheduler (macOS
  launchd · Linux crontab · Windows schtasks) that runs `saas sync --if-due` on a cadence; idempotent, zero deps.
  `saas sync` now snapshots vast.ai GPU every run so a frequent schedule captures short-lived/destroyed instances.
- **Worklog / 4(+2)-category model** (`scripts/slack/worklog_canvas.py`, server `worklog_pull.mjs`) — per-org,
  two-part (finance + team) rollup over the canonical model: ① LLM API (provider×model) · ② remote compute
  (provider×machine) · ⑥ infra/B2 = hard $; ③ est chat value · ④ est code-chat value (·⑤ cowork) = plan-covered
  estimate; + subscription line. Periods day/week/month/quarter/ytd, scope org/team/user. Sourced from the prod
  rollup + taxonomy (no stubs). Slack Canvas push prototyped via MCP.
- **Shared classifier** (`attribution.py`) — one `org → team × project` classifier + taxonomy for chat AND code
  (claudecode now classifies sessions per-content, not by cwd). `resources.snapshot()` records vast.ai instances so
  destroyed ones stay reconstructable; instance label→project via config `resources.vastai.label_map`.
- **Unified reconcile loop** (`reconcile.py`) — every spend source (LLM + GPU; subscription/storage as adapters are
  added) runs the SAME loop via a `Source` adapter: truth_total − captured = gap → agentic attribution (a caged LLM
  reads the conversations) → residual, **account-anchored** (only `owns_account` reconciles a shared account) with
  the unrecoverable remainder surfaced as an **explicit residual** (never dumped on a project/org). `reconcile all`
  prints the unified view. GPU destroyed-box recovery is now part of this: `resources discover [--agentic]` mines
  transcripts for instance identity + attribution. (Replaced the earlier conversation-alignment gap-spread, which
  could leak a shared account's gap cross-org.)
- **claude.ai chat adapter** (`spendguard chat test|show|discover|classify|work|story|sync|enable`, `chat.py`) —
  **OPT-IN, on-device, macOS** (Path 2). The desktop app caches no conversations locally (it fetches live), so this
  decrypts *your* `sessionKey` cookie (macOS Keychain → PBKDF2 → AES-128-CBC, Chromium format) and calls claude.ai's
  internal API to digest your conversations into the same **work-done + usage-value** rows (channel=`claude-ai`,
  billed=`false` — chat is on your plan). Incremental **watermark** by `updated_at`; 0600 cookie cache (no Keychain
  re-prompt). **Value counts ALL content** — uploaded files reviewed (input), files generated/edited via tools +
  thinking (output), not just the (often-empty) message text — attributed **per message-day** with a caching-aware
  per-turn model (prior context at the cache-read rate). **Agentic, generic attribution** (nothing hardcoded):
  `chat discover` reads your corpus and PROPOSES an `org → team × project` taxonomy (seeds with your current one,
  prints a diff for periodic review) → `chat classify` assigns each conversation `{org, team, allocation:[{project,
  pct}]}` (segmentation: a conversation's value SPLITS across the projects it touched → additive, no double-count).
  `org → team` is the additive scope tree; `project[]` is the orthogonal/multi dimension. `chat work` = rows by
  period, `chat story --run` = caged narrative + private work-insights. Both `discover`/`classify` are caged
  (`spendguard:categorize`, estimate-first). ⚠️ unofficial + ToS-grey; **push gated** behind `chat.enabled`, runs
  only on `chat sync`, org-routed to the matching connection. Token never logged / never leaves the machine.
- **Chat attribution LOOP + activation** (`chat loop|status|accept|push-taxonomy`) — one engine behind two
  activations. **User self-serve**: `chat enable` → `chat loop` (fetch new → classify unclassified → periodic
  discover/reallocate → sync), folded into `saas sync --if-due` so it runs on the existing cadence. **Org-requested**:
  the org enqueues an `attribute` command (dashboard) → the client pulls it on sync → `chat status` surfaces it →
  `chat accept` **consents** (enables + pulls the org's canonical taxonomy via `/v1/taxonomy`). The loop NEVER
  force-enables — org *requests*, member *consents*; it runs on the member's machine/session and only org→team×project
  *value* rolls up. Periodic taxonomy review (`chat.discover_days`, `chat.auto_taxonomy`) proposes + reallocates.
  `push-taxonomy` publishes a curator's local taxonomy as the org canonical (members then classify consistently).
- **`claude-code work --by day|week|month|quarter`** — the *real* work-done: conversation-derived ROWS (what was
  **asked** + value + tools/files per session), bucketed by period. Replaces the shallow git-commit count as "what
  the spend bought."
- **`claude-code story --by … [--run]`** — caged synth over the work rows → a narrative **story** + private
  **work-insights** (findings/decisions/gotchas/next — the WORK/domain knowledge, distinct from cost-efficiency
  learnings; never pooled). Estimate-first, capped by caps.meta.
- **Claude Code adapter** (`spendguard claude-code show|sync`, `claudecode.py`) — mines `~/.claude/projects/*.jsonl`
  into **spend + work-done**, so Claude Code usage shows next to API/batch/GPU even on a subscription (CC meters
  tokens regardless of billing). Per (project, model, day) cost ≈ tokens × canonical pricing (project = the session
  cwd) + work-done (tool counts: Edit/Write/Bash/…, files touched). **Incremental + idempotent**: a per-session
  **watermark** (`{lines, mtime}`) reads only NEW turns; a local per-day accumulator means `sync` pushes correct
  full-day totals (channel=`claude-code`) that upsert cleanly as conversations grow. (Note: on a plan the $ is
  usage *value* / API-equivalent, not literal billing.)

### Fixed
- **Deep-review pass** (portability + correctness): `resources.DEFAULT_LABEL_MAP` is now **empty** (the shipped
  vision/nlp-pipeline defaults silently mis-attributed a stranger's GPU); `iso_period` gained the missing **`ytd`**
  branch (was advertised but fell through to month) and is shared (was triplicated); `attribution.classify_items`
  prompt now requests `confidence` (was read but never asked → always 0); `_toklen("")` → 0 (was 1); genericized
  real project/org names leaked in `resources.py` docstrings; `claudecode.load_cls()` replaces hardcoded state-file
  reads; the reconcile gap is **spread across actual usage days** (was lumped on the reconcile day → daily≈monthly).
- **Token counts were stored as 0 server-side** for `claude-code` (and would be for `claude-ai`) — the adapters sent
  `in_tok`/`out_tok` but the ingest expects `in_tokens`/`out_tokens`, so token columns silently zeroed (spend/$ was
  always correct). Adapters now send the canonical names. Server `/v1/ledger` channel allowlist gains `claude-ai`.
- **`saas sync` now also pushes vast.ai GPU** (`resources.sync` folded in) — it was LLM-only, so remote-compute was
  never reconciled unless you ran `resources sync` separately. And `resources.sync` no longer 422s when a project
  has no attributed GPU (e.g. unlabeled instances) — it skips with a message pointing at the real fix (label vast.ai
  instances per project / set `resources.vastai.label_map`; destroyed instances are unrecoverable per-project).

## [0.2.8] — 2026-06-20

### Added
- **Coverage + pricing-drift push** (`saas.push_status`, in `sync`) — each contributor reports a scrubbed snapshot
  to the server's `/v1/status`: a `gated` bool (does this interpreter *auto*-enforce the gate at startup, probed in
  a clean subprocess so the CLI's own install doesn't mask it) and `{model, pct}` price-table drift vs OpenRouter.
  Powers the org dashboard's "X of N seats gated" panel + drift flag. Honors visibility + the contributor-email
  requirement; graceful if the server lacks the endpoint.
- **Batch-1 gate** — before a *large* batch for an intent that has **no recent realtime/batch-1 test of the same
  shape**, the gate now WARNS (prompts if interactive) — or hard-refuses with `GATE_REQUIRE_BATCH1`. The cost cap
  can only stop *over-spend*; it can't catch a prompt/tool bug in a correctly-sized batch — and the #1 batch waste
  is exactly that (a 1–5 item realtime test would've caught it for ~$0). This mechanizes the "PROMPT-CHECK →
  batch-1 before you scale" discipline instead of relying on it. Heuristic + opt-out so it never breaks a legit
  job by default. Signal = a recent realtime call for the same intent in the call corpus (`calls.tested_recently`).
  Knobs: `GATE_BATCH1_MIN` (req count = "large", default 50) · `GATE_BATCH1_USD` (or ≥ this $, default 5) ·
  `GATE_BATCH1_DAYS` (look-back, default 14) · `GATE_REQUIRE_BATCH1` (refuse non-interactive) · `GATE_NO_BATCH1`
  (off) · `GATE_ALLOW=1` bypasses.

## [0.2.7] — 2026-06-20

### Added
- **`import spendguard` now actually gates** — closes the #1 adoption gap ("pip install ≠ gated"). Previously, the
  common `pip install llm-spendguard` + `import spendguard` path patched *nothing*, so spend went ungated SILENTLY
  while the user thought they were protected. Importing the guard now installs it (idempotent, fail-open).
  - `SPENDGUARD_NO_AUTOINSTALL=1` — opt out of the import-time install (you call `install()`/`require()` yourself).
  - `SPENDGUARD_REQUIRE=1` — **refuse loudly when ungated**: upgrade the import to fail-closed, so if an LLM SDK is
    present but the gate can't enforce here (wrong interpreter, or `spendguard off`), the import RAISES instead of
    letting you spend ungated. Lets a team enforce with one env var, zero per-script edits. No-SDK contexts (e.g.
    running the `spendguard` CLI) stay a no-op.
- **`spendguard init --quick`** (`--yes`/`-y`) — non-interactive setup: writes sensible defaults with zero prompts
  (CI / fast onboarding). Implies local-only unless `--connect` is also passed.
- **Key pre-flight in `spendguard init`** — after setup, init now reports whether `OPENAI_API_KEY` /
  `ANTHROPIC_API_KEY` actually RESOLVE in this interpreter (🟢/🔴), the same check as `spendguard doctor`. This is
  exactly the silent gap that blinded reconcile/report after a repo move (cwd-relative `.env` lost the keys).
- **Louder estimate-only banners** — every caged, estimate-first command (`optimize`/`mine`/`reconstruct`/`review`/
  `experiment`/`promote`/`conv`/`cache-test`/`cascade`/`bootstrap`) now prints one consistent, hard-to-miss
  "🟡 ESTIMATE ONLY — nothing was spent · re-run with --run" banner (with projected $ when known) instead of a quiet
  one-liner, so a dry run is never mistaken for a real one. (`spendguard.ui.estimate_only`.)
- **Contributor-email requirement when pushing to a team** — when SaaS is enabled and `visibility` isn't `private`,
  the client now REFUSES to push un-attributable rows if the contributor isn't an email (the server bills/rolls up
  by email; an anon `usr_<hex>` would create a phantom member). `push_rollup`/`push_workdone`/`push_insights`/`sync`
  skip with a clear one-line fix (`spendguard saas link`); `saas status` + `doctor` show a 🔴 flag. Solo/local
  dashboards opt out with `SPENDGUARD_ALLOW_ANON=1`.
- **`spendguard workdone --push`** now feeds the server's `/v1/work` (`saas.push_workdone`) — the work-done roll-up
  (git commit subjects + LLM batch-intent counts per month·project) lands on the team/org dashboard next to spend.
  Monthly periods, filtered to the connection's project(s), visibility-honored, graceful if the server lacks the
  endpoint. (Previously `--push` called a non-existent function and crashed.) Configure your repos via
  `workdone.repos` in `saas.json` — `DEFAULT_REPOS` is intentionally empty in the public repo.
- **`reconcile_realtime` + everything in `sync`** — `reconcile_realtime` backfills the gate's realtime history
  (`realtime_log.jsonl`) into the ledger as `realtime` rows = `max(0, log − gate-recorded)` per (provider, day),
  idempotent — closing the gap where realtime logged before the sqlite ledger backend never reached the roll-up.
  `sync()` now reconciles **realtime alongside batch** and pushes **work-done** too, so batch + realtime spend and
  work-done all roll up to the org automatically on every sync — no manual `--push`. (`record_reconciled`/
  `clear_reconciled` generalized to take a marker; realtime markers `(realtime-history)` rebuild idempotently.)

### Fixed
- **Cross-account misattribution in `reconcile_into_ledger`.** A connected client now only reconciles the shared
  provider-account gap when it **owns** the account (`owns_account=true`). Previously *any* connected repo that ran
  reconcile claimed the whole OpenAI/Anthropic account's no-evidence batch spend under its own project — so a repo
  sharing the account (e.g. a vision pipeline) absorbed another repo's LLM batch. Non-owning connections now skip
  the gap entirely (the owner connection absorbs it); standalone/unconnected use still reconciles fully.

### Changed
- Corrected stale SaaS URLs in docs / examples / skill / comments (`llmseg.ai` and the Vercel preview URL →
  the canonical `https://llmspendguard.com`). No behavior change — the client default URL was already correct.

## [0.2.6] — 2026-06-18

First public release. Same gate + advisor; this cut genericizes the repo for open source.

### Added
- **`spendguard init --chat`** — optional conversational setup: ONE small realtime call on YOUR own key, caged
  under `caps.meta` (intent `spendguard:init`, estimate-first, never the server), parses plain-English budgets
  ("$2k/mo for LLMs and $800 for GPUs") into `caps.llm/compute/total`. Falls back to the deterministic prompts
  if no key / the call fails. Default `init` stays deterministic + zero-LLM.
- **`init` now points to the corpus bootstrap** (`spendguard bootstrap` / the `/spendguard-learn` skill) to seed
  the advisor from past provider history on day one.
- **Coverage 19% → 35%.** The subprocess test runner supports `SPENDGUARD_COVERAGE=1`; coverage now attaches at
  interpreter **startup** via a `process_startup()` `.pth` hook + `COVERAGE_PROCESS_START`, so code the gated
  venv's sitecustomize imports before the tracer would otherwise attach is counted (`__init__` 0→100%, pricing
  17→54%, gate 44→55%). New **offline** unit tests for the formerly-untested CLI/mining/advisor modules —
  `adapters`/`audit`/`backfill`/`bootstrap` 100%, `ledger_sync`/`advise` 98%, `workdone` 97%, `reconcile_openai`
  87%, `reconcile_anthropic` 82% (every provider/network call stubbed — no spend). CI floor raised `15 → 30`.
- **More gate fail-closed tests** (`tests/test_gate_failclosed.py`) — `require()` refuses when disabled / not
  enforcing; the real-time precheck refuses over `GATE_RT_BUDGET`, honors `GATE_ALLOW`, and `GATE_DISABLE` passes
  through (kill switch). All offline (SDK create methods stubbed; no network, no spend).
- **Docs site** — MkDocs Material (`mkdocs.yml`), home is a 60-second [quickstart-as-tutorial](docs/index.md);
  Architecture / Using-with-Claude / Learning-advisor / Roadmap wired into the nav with Mermaid + dark mode.
  **Brand-skinned to match llmspendguard.com** (`docs/stylesheets/extra.css`): warm cream + teal palette,
  editorial Newsreader serif headlines over a system-sans body, shield logo. Published to GitHub Pages via
  `.github/workflows/docs.yml` (strict build); deps pinned in `requirements-docs.txt`.
- **Ruff** lint in CI (`select = ["F","B"]`) — correctness/bug lints; format intentionally *not* imposed (keeps
  the dense, deliberate one-liner style readable). **Release workflow** (`release.yml`) publishes to PyPI on a
  `v*` tag via trusted publishing.
- **ARCHITECTURE.md** rewritten around the extensibility seams (extend, don't fork), with diagrams.
- **Public-release cleanup** — genericized all internal example references (project tags, org names, sample
  emails) to neutral placeholders (`nlp-pipeline` / `vision-pipeline` / `acme` / `you@example.com`); project
  auto-detection keyword maps are now generic illustrations to customize. Behavior unchanged; full suite green.

## [0.2.5] — 2026-06-16

Split caps by resource class + a public-consumption documentation pass.

### Added
- **Split caps by resource class.** Cumulative caps are now per class, each with a `daily` and `monthly`
  window: `caps.llm.{daily,monthly}` (**HARD — gate-enforced**, OpenAI + Anthropic), `caps.compute.{daily,monthly}`
  (**alert-only** — remote-compute / vast.ai launches don't pass through the gate, surfaced in the report +
  dashboard), and `caps.total.{daily,monthly}` (the overall LLM + compute ceiling). Env overrides for each:
  `GATE_LLM_DAILY` · `GATE_LLM_MONTHLY` · `GATE_COMPUTE_DAILY` · `GATE_COMPUTE_MONTHLY` · `GATE_TOTAL_DAILY` ·
  `GATE_TOTAL_MONTHLY` (`config.class_cap`, `config_schema.py`, `resources.compute_exceeded`). The **legacy flat
  `caps.daily` / `caps.monthly` still work** and are honored as the total ceiling.

### Changed
- **Public-docs pass** (no logic changes): `llmspendguard.com` links throughout (README hook, docs index,
  pyproject `Homepage`); a new **"Why llm-spendguard?"** section; explicit **SaaS-status clarity** (the client
  is production-ready and standalone; the team/org server is a separate repo in development) in the README,
  ROADMAP, and the `/spend` skill; a **"Smart attribution"** subsection (WHO `org→team→contributor` × WHAT
  `project·intent·resource`); a stronger **conversational `spendguard init` / set-up-with-Claude** story; a
  clearer **extend-to-any-SDK** path (`register` + adapters + emit, zero deps, fail-open); a **"Getting help"**
  community footer (Issues, Discussions, site); and the PyPI install path alongside `pip install -e .`.
- **New `scripts/README.md`** documenting `bootstrap-remote.sh` (configuring a remote/ephemeral GPU box to
  gate + attribute + push), with prerequisites and an example.
- Code comments noting that the example project→path mappings (`workdone.py`) and project-detection keyword
  patterns (`conv.py`) are tuned to the author's machine and should be customized.

## [0.2.4] — 2026-06-14

Stand the repo on its own + simplify the SaaS seam.

### Changed
- **Relocated out of the consumer-repo tree** to its own directory (`~/Documents/claude/llm-spendguard`). It was
  always its own git repo, but was physically nested in a consumer repo and the gate hooks hardcoded that path.
  Re-pointed the editable install, both `usercustomize` hooks (system + intel python), the batch helper, and the docs/memory.
- **SaaS config simplified to ONE key.** Dropped `team_id`/`org_id` from the client — the server maps the
  Bearer `api_key` to the user→team→org hierarchy. Less to leak, nothing to keep in sync.

### Added
- **`saas.sync_interval`** (`off`|`hourly`|`daily`|`weekly`, default `daily`) — configurable push cadence.
  `spendguard saas sync --if-due` is cron-safe (pushes only when the interval elapsed; `last_sync` tracked in
  `saas_state.json`) and is wired into the daily `report` so the roll-up goes up on schedule automatically.

## [0.2.3] — 2026-06-14

Multi-interpreter coverage + the team/org SaaS client seam (ready to connect to the future server repo).

### Added
- **`spendguard coverage`** — the gate is per-interpreter, and most people run several pythons (3.11, 3.14,
  venvs). This scans every interpreter on the machine (bounded — no recursive `$HOME` walk), reports which
  can actually **import** the LLM SDKs and which are **GATED**, and prints the exact `install-hook` line for
  any gap. "has SDKs" now means *importable* (arch-mismatched installs like intel pydantic on arm64 no
  longer show false positives). Exit 2 if any gap.
- **SaaS client seam** (`saas.py`, `spendguard saas`, `saas.example.json`) — points at the future SEPARATE
  server repo (llmspendguard.com). Config in `~/.spendguard/saas.json` (gitignored) or env: `enabled`, `url`,
  `api_key` (secret), `team_id`, `org_id`, `visibility`. Speaks a documented `/v1` contract
  (`health`/`ledger`/`insights`) with Bearer auth; **degrades gracefully until the server exists**;
  `visibility=private` = nothing leaves the machine. Partner, not supervisor — never overrides local caps.
  New `saas`/`coverage` config section + `saas.json` store wired through `config`/`init`.

### Changed
- `scripts/batch_llm.py`: `estimate_both` → **`multi_llm_estimate`** (it always took N models, not 2);
  `estimate_both`/`dual_estimate` kept as back-compat aliases.

## [0.2.2] — 2026-06-14

Close the **generation-time** bypass: make assistants write gated code, and gate PEP668 system pythons.

### Added
- **`spendguard install-rule [--global | --project DIR]`** — writes a standing rule into `CLAUDE.md` (a
  marked, idempotent block) so **every** Claude/Cursor conversation in that scope is told to route the LLM
  code it builds through spendguard (gated interpreter + `require()` + canonical pricing + estimate-first).
  New doc: [`docs/USING-WITH-CLAUDE.md`](docs/USING-WITH-CLAUDE.md).
- **`install-hook --user --python <interp>`** — gate another interpreter's user site via a **path-injecting
  `usercustomize`** with **no pip**, so it works on PEP668 "externally-managed" pythons (Homebrew/system).
  Fixes the real-world `--user` failure on managed system python.

### Changed
- `install-hook` verification now reports `ENFORCING` (checks the SDK method is actually patched) for the
  target interpreter, not just "importable".

## [0.2.1] — 2026-06-14

Hardening pass after an adversarial code review (three independent reviewers).

### Fixed
- **Fail-open** (critical): gate_fns now run via `_guard` — only `SpendGateRefused` propagates; any other
  error (e.g. `database is locked` under fleet concurrency) logs and lets the call proceed. Also protects
  third-party `register()`'d gate_fns.
- **Anthropic real-time cost** was undercounted ~2× — `input_tokens` excludes cache reads, so the cost
  formula double-subtracted them. Normalized to OpenAI token semantics before pricing.
- **Provider classification** — by `startswith("claude")`, so o-series/embeddings attribute to OpenAI.
- **Cage via CLI** — `cli.main()` now calls `install()` so the advisor's own LLM calls are caged even
  when run via the CLI outside a gated venv.
- **Real-time-budget "allow"** now bypasses only the RT budget (process-local flag), not the per-batch /
  daily / monthly caps.
- **CI price audit** now actually gates the build (removed `|| true`, fixed the call, audit skips its own
  examples); CI runs the full `pytest`.
- **Cost math** clamps cached tokens ≤ input (a bad usage object can no longer inflate cost).

### Added / changed
- Tests for the money-critical core: `pricing`, `reconcile`, `submit`/`estimate` (now 16 test modules).
- `--semantic embed|rubric` equivalence now applies to JSON too (was silently skipped).
- Honest types on the public API (`py.typed` is no longer a lie); honest output in `validate`/`cascade`
  about which signals are coarse heuristics vs proven.
- **Docs:** `docs/ARCHITECTURE.md` (diagrams) + `CONTRIBUTING.md`.

## [0.2.0] — 2026-06-14

The release that turns the cost *gate* into a cost *governor* — it now learns the cheapest config
that keeps quality, and helps you find + prove efficiency wins.

### Added — learning advisor (#6/#7)
- **Per-call corpus** (`calls`): opt-in cost+quality record per call/intent, deferred quality
  (implicit "used" / explicit `feedback`), `spendguard calls` → cost-per-good-result.
- **Advisor** — `advise`/`backtest` (deterministic, no spend), and caged LLM ops `mine` (insights),
  `optimize` (recommendation), `review` (practice audit). All tagged `intent=spendguard:*` and capped
  by a **separate meta budget** (`caps.meta`, default $2/day), excluded from the corpus they analyze.
- **Living insights** (`validate`): conditional, context-rich, lifecycle-tracked (candidate→active→
  refuted/superseded) — re-validated as data grows.
- **Collective learning** (`insights export/import`): opt-in, **scrubbed** (abstracted) rules in,
  low-trust community priors out — corroborated locally before they sway the advisor.
- **History mining** (`mine-history`, `mine-conv`): reconstruct intents from repo artifacts + a graph;
  mine session transcripts for the cost playbook.
- **`bootstrap`**: one cold-start command that mines all history into a ready corpus.

### Added — quality corpus & efficiency lab
- **`fetch-io`**: recover real prompt+output from providers (OpenAI batch files / Anthropic results),
  free, into a bounded `call_io` sample → makes `good%` / `$/good` real.
- **`experiment`**: A/B/n lab — variants vs a baseline on real samples, measuring cost **and**
  output-equivalence (graded `equivalence` ladder: exact→scalar→text; opt-in `--semantic` embed/rubric),
  **graduated** (pilot→kill losers cheap→expand→report ±stderr) to beat the law of small numbers.
- **`promote`**: run a winning config and KEEP the output as production (work-not-wasted); realtime or
  `--batch` (Batch API, 50% off) for large chunks. Workload-tagged.
- **Per-model learnings** (`models`): family rules + verified facts auto-applied on every call
  (gpt-5.5→reasoning='none', mini/nano→'minimal', cache minimums) with self-heal; a **soft denylist**
  (a model killed at the pilot is auto-skipped for that intent, `--reconsider` to retest).

### Added — cost levers & integrations
- **Prompt caching**: `cache-audit` (find reusable prefixes), `cache-test` (prove it engages + measure).
- **Semantic cache / dedup** (`semcache`, `dedup`): opt-in response cache + batch dedup (within-batch +
  cross-run/retry) — avoid re-paying for completed work.
- **Cascade routing** (`cascade`): cheap→verify→escalate (FrugalGPT-style), denylist-aware.
- **Observability**: OTel **GenAI semantic conventions** (metrics + spans) → any OTLP backend
  (Langfuse / Helicone / Phoenix); webhook + in-process callback.
- **Pricing**: `cross-check` vs OpenRouter's public JSON (table now cross-checked by LiteLLM + OpenRouter).

### Packaging
- Renamed distribution to **`llm-spendguard`**; full metadata, classifiers, `py.typed`, optional extras
  (`openai`/`anthropic`/`otel`/`all`/`dev`), `pytest` runner over the suite.

## [0.1.0]

- Pre-spend **gate** (OpenAI/Anthropic SDK overlay) with hard caps + human approval + kill switch.
- Canonical **pricing** table (gpt-5.5 $5/$30 realtime · $2.50/$15 batch; opus-4.8 $5/$25 · $2.50/$12.50),
  layered from LiteLLM + curated + override, with a price-literal audit.
- **Reconcile** (OpenAI/Anthropic batch), daily/weekly/monthly **report** + email, cross-process
  SQLite budgets, declarative config registry + guided setup.

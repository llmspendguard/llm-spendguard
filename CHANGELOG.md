# Changelog

All notable changes to **llm-spendguard**. Format loosely follows Keep a Changelog; dates are UTC.

## [Unreleased]

## [0.12.14] — 2026-10-09

### Fixed
- **lane_queue moved OUT of the spend.db money ledger into its own database.** Measured 2026-10-08, the lane_queue
  table was 2.76GB = 63% of a 4.4GB ledger; the drain's `purge()` ran every cycle inside `BEGIN IMMEDIATE` (exclusive
  ledger lock) with no index on `updated_ts`, scanning all 137k terminal rows — producing ~4-min 100%+ CPU drains, a
  1.3GB WAL that never checkpointed, 3.3→4.4GB ledger growth, a 21GB snapshot dir (each snapshot 63% queue), and tool
  calls stalling on the lock. The queue now uses `config.lane_queue_db_path()` (`~/.spendguard/lane_queue.db`);
  `config.pooled_ledger_conn`/`fresh_ledger_conn`/`ledger_op` take an optional `path`. `spendguard lanes
  --migrate-queue-db` moves live rows + drops the old table (reclaims ~2.7GB).
- **Drain purge is bounded + decoupled.** New `(state, updated_ts)` index; chunked set-wise delete (short txns that
  release the lock, bounded memory); `purge_due()` runs purge at most hourly (`advisor.queue_purge_min_interval_s`)
  instead of every drain cycle.
- **WAL no longer grows unbounded.** `journal_size_limit=64MB` added to `tune_ledger_connection`, so a checkpoint
  truncates the `-wal` file instead of leaving it at its high-water mark.
- **Drain non-overlap.** The drain takes a non-blocking single-instance `fcntl` lock; a second drain is refused rather
  than run concurrently.
- **`spendguard deploy` is single-instance.** Generalised the drain's lock into reusable
  `config.single_instance_lock(name)` (non-blocking cross-process count-1 semaphore via `flock`, released on fd close
  so a crash leaves no stale lock); the deploy command holds it across the whole gate+promote. Two concurrent gates
  each spawned a full `chunked_suite` and starved each other's CPU, flaking the receipt/timeout-sensitive tests
  (measured 2026-10-09); a second invocation now fails fast with a clear message instead of piling on.
- **Log spam.** The operator lane summary + fallback/eligibility alerts are suppressed in `--drain` (daemon) mode, and
  the best-value advisory routes through `warn_once` (it had written a 15MB `lane-drain.log`).
- **`lane_queue_archive.jsonl` is bounded** (rotated past `advisor.queue_archive_max_mb`, default 64MB; was 820MB).
- **`.corrupt` quarantine copies bounded** (`safety.corrupt_keep`, default 3 per base file; 142 had accumulated), and
  **`config.update_json` stages each write in a unique per-writer tmp file** — the shared `<name>.tmp` let concurrent
  processes interleave and promote torn JSON, the root cause of the 140 `resource_state` corruptions.
- **Embed resume-checkpoint dir bounded** (`gc_embed_checkpoints`, age + total-GB; 19.65GB had accumulated).
- **Ledger snapshots throttled** to one/day (`safety.snapshot_min_interval_hours`), `safety.snapshot_keep` 4→2.
- **Evidence-truncation fixes**: `estimate_literals` sends the whole enclosing function to the price adjudicator;
  `conv.classify_evidence` / `attribution` and `realtime_find_batch` send whole chunks packed by budget, not a head cut.

## [0.12.13] — 2026-10-07

### Added
- **Plan-axis admission — a $0 subscription call is now governable.** Billed-$ caps (`caps.intent_caps`,
  `caps.llm.daily`, caller `*_SESSION_USD`) are structurally blind to plan burn because a plan-lane call books $0, so
  a capped plan could drain unchecked (measured: 139 `claude-code` calls / 12.3M input tok / `$0.0000`, `deferred:
  0`). New `plan_admission.decide()` governs plan burn in its OWN axis (plan quota / paid-overage), NOT dollars: a
  labelled, UNPINNED `adapters.call` whose plan lane is capped or on paid overage is redirected to a pre-confirmed
  READY substitute lane, or REFUSED (`PlanAdmissionRefused`) when none is ready — never failing open onto the
  exhausted plan. Caller pins (`no_substitution` / `metered_only` / `measurement`) bypass admission entirely — the
  hard substitution contract is unchanged. Threshold is config (`plan_admission.remaining_pct` /
  `SPENDGUARD_PLAN_ADMISSION_REMAINING_PCT`). `spendguard doctor` and `receipt` surface the plan axis as a
  first-class risk line, kept separate from billed dollars.

### Fixed
- **A redirected call now records WHY.** Lane/model redirects wrote `fell_from=NULL`, `retry_of=NULL` and were
  indistinguishable from a genuine request for the served model — the reason a 48h substitution drift was invisible.
  The calls ledger gains `requested_model` / `served_model` / `resolved_lane` / `redirect_reason` (additive columns,
  not overloading `fell_from`), populated for bandit / load-balance / fallback-ladder / lane-unavailable / tier-
  mapping / plan-admission redirects. Caller attribution now walks PAST the worker-thread trampoline
  (`thread.py`/`threading.py`/`concurrent.futures`) to the real originating frame, so a burst is attributable to the
  code that caused it rather than `thread.py:run`. Guard: `tests/test_plan_admission_and_redirect_provenance.py`.
- **Codex lane auth uses the token's own exp as authoritative** (carried from `55b8459`): a non-zero `codex login
  status` while the on-disk token is unexpired is a transient (a status subprocess racing a token refresh), not a
  logout, so it no longer fires a false "re-login" banner on a valid token.
- **An un-honorable effort pin AUTOTRANSLATES instead of crashing the call.** Guardrail A used to `raise
  EffortNotHonored` when a caller pinned a model (`no_substitution`) that cannot express the requested `reasoning`
  (e.g. `minimal` on a model whose floor is `none`). That refusal crashed every commit's honestreview precommit
  review once a plan hit paid overage and `advisor_model` resolved to such a model. spendguard now floors the effort
  to the model's supported value, records the mismatch loudly (`note_unhonored_effort` + the ledger's real-effort
  row + guardrail-D's dollar cap — the doctrine's "never silent" requirement), and PROCEEDS. A measurement that needs
  exact effort reads requested!=applied and discards. Internal confinement pins already floored; a direct
  `no_substitution`/`metered_only` now floors the same way (path-independent).

## [0.12.12] — 2026-10-07

### Fixed
- **Codex lane flapped to a false "logged out" banner under process contention.** A single non-zero
  `codex login status` (many codex spawns racing on the auth/refresh file) was reported as a confirmed logout,
  firing a persistent "re-login" banner that re-notified every 30 min until a successful call cleared it — while the
  ChatGPT plan token was in fact valid. Now `codex_exec.auth_status` CONFIRMS a non-zero exit with one re-check
  before reporting `authed=False` (a real logout stays non-zero on both; a transient recovers; `None` still on
  timeout/exception), and `lanes.lane_auth_status` clears a stale `event-auth` banner the moment it observes
  `authed=True`, so a re-login (or any positive re-check) self-heals on the spot rather than only on a later served
  call. This keeps the $0 codex lane usable instead of falling back to the metered API on a phantom logout.
- **The deployed-posture check in the consensus-panel test treated "no live config" as a failure**, reddening
  release CI on keyless runners while passing locally; absence of a deployed `~/.spendguard/config.json` is now a
  genuine n/a (the code behaviour checks still gate; the deployed checks still fire where a config exists).

## [0.12.11] — 2026-10-07

### Fixed
- **Warm codex lane hung 45s on every turn, so no codex call ever succeeded.** The warm `codex app-server` turn
  waiter keyed on a `(thread_id, turn_id)` that a tiny turn's `item/completed` notification can reach BEFORE the
  `turn/start` response that reveals the turn id — so the completion was dropped (no waiter installed yet) and the
  turn waited out `CALL_TIMEOUT_S`. Racing notifications are now retained in `_pending_turn_notifications` and
  replayed the moment correlation exists (`_correlate_turn_waiter`). A headless app-server also no longer boots the
  user's 8 MCP servers on the thread (`-c orchestrator.mcp.enabled=false`). A consequence: a "codex lane logged out"
  health banner self-heals again — it clears only on a *successful* codex call, which the hang had been preventing.
- **Bandit denylist drift silently unprotected a cross-vendor consensus panel.** An external writer could replace
  `advisor.bandit_denylist` without updating `advisor.bandit_denylist_sources`, dropping a source-credited pin. The
  reader now self-heals: `_effective_bandit_denylist()` is the UNION of the list and the sources-map keys, so every
  source-credited pin stays effective regardless of drift. `register_critical` remains the writer; the empty-list
  optout default is preserved.
- **Receipt invented `0%/0%` cache axes when a scope had no cache-token data.** `_cache_split_line` now gates on
  actual cache-read/cache-write token evidence and states "no measured token data for this scope" instead of
  rendering a measured-looking zero. (Recording was already wired end-to-end: `adapters._call_once` →
  `cache_read_tok`/`cache_write_tok` ledger columns → `budget.input_cache_split`.)
- **Compaction nudge fired every turn and claimed a false "~1x cheaper" saving.** The Stop-hook nudge now fires once
  per threshold crossing (debounced, re-firing only after context grows materially), and the unsupported savings
  multiplier is gone — it reads `⚠ NNNK tok/turn · /compact (guided)`.
- **PreCompact hook emitted a schema-invalid payload** (`hookSpecificOutput` is not allowed on PreCompact). It now
  emits only `{"suppressOutput": true}` and the post-compaction preservation digest is re-injected via the
  SessionStart(`source=compact`) hook, which does support `additionalContext`.
- **Delegated provider spawns inherited an untrusted cwd and an open stdin**, guaranteeing a 300s hang for the
  capped-plan escape hatch. All cold provider spawns now set `stdin=DEVNULL`, the exec/daemon layer no longer reads
  ambient `os.getcwd()`, and agentic/workspace-write delegations require an explicit absolute cwd.
- **Batch doors could submit N requests that all 400, and could book phantom spend.** Two fixes. (1) **One param-name
  authority** — the OpenAI output-budget parameter name now comes from `models.tokens_param()`, read by BOTH the
  realtime path (`adapters._call_once`) and the Batch builder (via `apply_call_params`). It returns
  `max_completion_tokens` for every chat / OpenAI-compatible model, **regex-independent**, so an unlisted family
  (gpt-6.1-sol — or kimi-k3 / glm-5.2, which match no family rule) can no longer inherit the legacy `max_tokens` and
  hard-400. This closes the drift that 400'd **1,930/1,930** requests of a real gpt-6.1-sol batch (realtime sent
  `max_completion_tokens`; the batch door sent `max_tokens`). (2) **Mandatory one-request pre-flight at every batch
  door** — `submit.guarded_submit` (OpenAI chat + embeddings) and `submit.submit_message_batch` (Anthropic) now send
  the FIRST built request live and require a 2xx **before** `.batches.create` and **before** the cost estimate is
  booked, so a model-wrong param / rejected reasoning_effort / unservable `response_format` / auth failure / stale id
  surfaces as ONE clear error — not N — and a 0-success "completed" batch can never settle as phantom spend. A
  stale/unknown id carries the agentic "did you mean" hint (`vendor_call.closest_served`). Opt out with
  `preflight=False`.

### Added
- **Agent-spawn gate** — a near-cap Anthropic plan reroutes a reroutable `Agent`/`Task` spawn off the capped lane
  instead of silently billing overage.
- **Warm codex daemon rewired to `codex app-server`** (codex 0.160.1 removed the `mcp-server` subcommand), the
  substrate for the warm-lane turn fix above.

## [0.12.10] — 2026-10-06

### Fixed
- **`embed()` false "batch rejected" on a group smaller than the batch size** — `adapters.embed` compared the largest
  successful sub-group `w` to the batch size `_n`, so a fully-successful group with FEWER inputs than `_n` (a 1-input
  query embed, or any partial final chunk) read as a provider rejection: it logged "batches of N were rejected; the
  workable size here was <w> — running the rest at <w>/request" and spuriously shrank `_n`, though nothing was
  rejected. Now compares `w` to the size actually attempted, `len(grp)` — a real rejection is `w < len(grp)` (the
  group had to bisect) — and the warning reports that attempted size. No cross-call state is involved; the batch size
  is per-call, and `text-embedding-3-large` already carries `capabilities.embed_max_batch = 2048`.
- **SDK-surface gate test no longer flakes on run-order** — `test_every_sdk_surface_that_spends_is_gated` now arms the
  gate (idempotent `gate.install()`) before asserting, so a sibling test that reloads an SDK submodule can't leave a
  surface transiently unwrapped and red the gate by chunk-order.
- **`spendguard doctor` no longer live-pulls on its default path** — the metadata-backbone health check (whose
  capability-completeness leg fetches the org `/v1/models`) and its empty-cache auto-heal `sync()` ran on the DEFAULT
  `doctor` path, so whenever that server was slow/unreachable `doctor` blocked ~1.4–2.6s and blew its own `<2s`
  health-check budget — the single test that reliably reddened the deploy gate. Now, like the ledger-leak check beside
  it, the full networked backbone audit + auto-heal run only under `doctor --live`; the default path reads the LOCAL
  cache freshness alone (`metadata_audit._cache_health`, $0, no network, no mutation). `test_gate_cli` is hardened
  from a flaky `<2s` timing proxy into a deterministic assertion that the default path makes no backbone pull, so the
  blind guard that let this latent live-pull ship cannot do so again.

## [0.12.9] — 2026-10-06

Delegate agentic-readiness fix + warm Codex daemon default — makes `spendguard delegate` **correct** (no false refusal
of an authenticated agent) and **warm** (~5s, not a >75s cold start). Fixes the false-negative observed live: an authed
`codex exec` was refused as "unavailable" merely because the $0 one-shot lane probe was down.

### Fixed
- **Delegate agentic false-negative** — `delegate_router.delegation_lanes_ready` now splits readiness by ROUTE KIND.
  An agentic route (runs the provider's agent CLI via `codex_exec.run_prompt`) is ready iff that CLI is present AND
  authed — reusing the executor's own `codex_exec.available()` + `auth_status()`, not inventing a second probe — NOT
  the $0 one-shot lane probe, which remains the readiness test for the one-shot route only. `delegate_task` classifies
  FIRST, then checks readiness for that route kind. An authenticated agent CLI is never refused because the $0 one-shot
  lane is down; a genuinely unauthenticated CLI still gets the typed refusal (never fail-open onto the capped Claude
  plan). The doctor line now reports both route kinds.

### Changed
- **Warm Codex daemon default-on** — the persistent `codex mcp-server` (`codex_daemon`) is now the default for the
  codex lane (opt out via `SPENDGUARD_CODEX_DAEMON` / `advisor.codex_daemon`), so an agentic delegation is ~5s warm
  instead of a >75s cold start. `run_warm` now serves workspace-write with an explicit per-call `cwd` (verified live:
  it writes files, not a no-op) and reclaims the idle subprocess after a timeout. Any daemon failure falls through to
  a cold `codex exec` — latency changes, never availability.

## [0.12.8] — 2026-10-05

Ledger attribution + ensure-success routing — the accounting-trust + cost-safety follow-up to a heavy real workload's
forensics. Makes "whose votes are these?" answerable, settles the `metered_only` question (no leak), and guarantees a
task always succeeds while never billing silently.

### Added
- **Ledger attribution dimensions** — `calls.call_class` (`workload` / `gate_internal` / `probe`) and
  `calls.origin_session` (per-process id), additive (NULL on legacy rows). spendguard's own meta calls stamp
  `gate_internal` even under a workload intent, so a caller's votes, gate-internal calls, and another session's work
  are no longer conflated under one shared intent tag. Groupable/filterable in the spend views.
- **Lane-quota capture + headroom-aware shedding** — remaining quota is captured per lane where the provider exposes
  it (honest `unknown` for kimi/zai, never fabricated), surfaced in `spendguard lanes --usage`; the router prefers a
  ready lane with more measured headroom.
- **`spendguard overage` CLI** — parity with the `spendguard_overage_status` MCP tool (was MCP-only; the CLI used to
  mis-suggest the unrelated `coverage`).
- **`--refuse-billed`** on `ask` / `delegate` / `comprehend` / bulk — an explicit opt-in "$0-or-fail" guard.

### Changed
- **Ensure-success routing** — a lane miss never silently fails and never silently bills. A FUNGIBLE call substitutes
  only to a $0 lane whose MEASURED quality holds (the advisor's agentic `meets_bar` verdict, no proxy/cutoff),
  exhausting quality-equivalent $0 lanes before a metered last resort; a PINNED call
  (`model_for` / `no_substitution` / `metered_only`) keeps its EXACT model and runs it metered if its lane is down.
  Any metered fallback is loud and carries `fell_from` — never a silent charge.

### Fixed
- **Batch booking** — a REFUSED submit now books zero rows (kills a confirmed $12.77 double-book); `collect`
  reconciles the submit-time estimate to the measured actuals in place.
- **`metered_only` audit** — confirmed NO lane leak: a `metered_only` call records `executor=NULL`, never a lane
  (direct and bulk, happy path and lane-down).

## [0.12.7] — 2026-10-05

**`spendguard delegate`** — the 2-hop that offloads a self-contained sub-task off a capped Claude plan onto a cheaper
or $0 subscription — plus a batch-submit 404 fix.

### Added
- **`spendguard delegate "<task>"`** (CLI) + **`spendguard_delegate`** (MCP tool): classify the task on a $0 lane
  (oneshot | agentic | needs_claude; provider from `advisor.lane_models`), then route — oneshot → `lane_balance.delegate`,
  agentic → the provider's agent CLI (`codex_exec` workspace-write) — or refuse `needs_claude` (never fail-open into the
  capped Claude call). Estimate-first dry-run by default; `--yes` / `execute=True` runs it. The receipt splits
  real-API-$ / plan est-value / avoided-overage-$ (ledger-measured rate) and never sums them.
- **Lane readiness**: `delegate_router.delegation_lanes_ready()` + a `spendguard doctor` line showing which delegation
  lanes are installed + authed, so a user with only some provider CLIs is routed to what they actually have (and told
  how to add more). Nothing is hardcoded to one machine's plans.
- `codex_exec.run_prompt(sandbox=…)` — an opt-in `workspace-write` agentic run (the read-only default is unchanged).

### Fixed
- **Batch builders 404** (`submit.build_message_batch_requests` + `build_chat_batch_jsonl`): the provider-prefixed model
  id (e.g. `anthropic:claude-haiku-4-5`) was sent to the vendor API verbatim, so every batch request 404'd. Both now
  strip to the bare id — the same strip every realtime path already does — while keeping the full id for pricing facts.
  Regression-guarded in both batch tests.

### Changed
- A shared CLI option-parser (`cli_arguments`) de-duplicates the `cli.py` / `lanes.py` option lookups.

## [0.12.6] — 2026-10-05

Reliability — the complete **429 storm → batch** system at the one admission chokepoint (`adapters.call → dispatch`),
building on 0.12.3's connection-window: a bulk fan is now **paced or diverted to the Batch API before it can storm**,
and implicit-fan coalescing (4b) is safely **default-on**. Validated live on the real metered API through both
admission doors (0 surfaced 429s, caller always served). No caller-side `metered_only`/`batch` tuning required.

### Added
- **Proactive cold cap** — the governor seeds `rpm`/`tpm` from the catalog's published limits
  (`model_catalog.rate_limit_for`), so a cold metered vendor is paced from call #1 (anthropic `0/0 → 1000 rpm / 2M
  tpm`) instead of admitting an unpaced burst.
- **`StormCoalescer` pace+batch combo + both admission doors** — `storm_submit.submit_storm` (explicit async entry,
  typed backpressure) and `storm_route` (the implicit raw-`adapters.call`-fan door, **default-on**, kill switch
  `SPENDGUARD_STORM_COALESCE=0`). Safe-on because it routes only the raw fan: explicit fans
  (`bulk_delegate`/`submit_storm`) pass `_coalesce_eligible=False`. Registry idle-TTL (120s) + cap (256) eviction so
  default-on can't grow one planner thread per call-shape.
- **Durable, crash-resumable, exactly-once batch** — `durable_batch_executor` on `lane_queue` + `batch_tracker`
  (`kill -9` proven); `row_results` now returns the originating prompt with each result, so an ordinal-into-a-candidate
  -list reply stays parseable hours later / in a fresh post-crash process.
- **`scripts/probe/live_validate_no_429_storm.py`** — both-doors live validation on the real metered path (honest
  intent+rowid ledger evidence; passes only when `rows_recorded >= N`, so a blind read can't pass vacuously).

### Changed
- Output-aware admission: `est_tokens` uses the learned `expected_output.expect` so OTPM isn't under-paced; AIMD
  (decrease-on-429 / increase-on-success) refines the floor; deadline-driven batch (`route_horizon.should_batch_fan`)
  + auto-chunk to the provider batch limits.
- `install-rule` §8 doctrine: when the Anthropic plan is capped, delegate agentic subwork to the codex/gemini/zai
  CLIs (regenerates the global + per-repo rule files).

### Fixed
- Internal 429s on the incident replay **931 → 0** (the shared cross-process sliding window has no burst allowance, so
  a fan ≤ rpm is paced from the first call — the per-request `_Bucket` that started full could not).
- DRY: one rate resolver (`dispatch.rate_per_s`), one governed-realtime executor
  (`storm_submit.governed_realtime_executor`), one advisor-knob reader (`config._advisor_coerced`).

## [0.12.5] — 2026-10-02

Adds `POST /embed` to `spendguard serve` — the embeddings analogue of `/ask`, so a non-Python caller (e.g. a
Node/Vercel app) can route **embeddings** through the gate (metered + priced + ledgered), not just chat.

### Added
- **`POST /embed` on `spendguard serve`** → `adapters.embed` (gate-metered, priced, chunked + durable). Request
  `{texts: [str], model?, dimensions?, max_batch?}`; response is `adapters.embed`'s contract — `{vectors (aligned to
  inputs, None where an item failed), model, dims, n, failed, error}`, so a partial failure is a usable `200`, never a
  silently-short list. A deliberate spend refusal (`BudgetRefused` / any `SpendGateRefused` cap breach) returns `402` —
  never a false `200`; invalid `texts` → `400`; the host-local `checkpoint` path is stripped from the response.
  `do_POST` was split into `_handle_ask` / `_handle_embed` (shared body read); `/ask`, `/health`, `/metadata` and the
  network-bind-needs-a-token guard are unchanged.

## [0.12.4] — 2026-10-01

Adds `spendguard keys-audit` — a static scan that catches a repo `.env` holding a provider key that would shadow the
keys.env SSOT at `load_dotenv()` time, the shadow `doctor` cannot see (it reads the live `os.environ`, not the files).
This is the gap behind the "warden 401": a rotated key stays stale in a repo `.env` and a real env var wins in
`api_key()`.

### Added
- **`spendguard keys-audit [path...] [--strict] [--json]`.** Reads `.env`-family files directly and, for each key whose
  name keys.env ITSELF declares, compares the file value to the SSOT: `differ` (the dangerous active-when-loaded shadow →
  exit 1) or `dup` (identical value, redundant → exit 1 only under `--strict`). A directory scans its root `.env` /
  `.env.local` / … ; an explicit file is read directly. Last-4 only — values are compared in memory, never printed.
  Whether a var is a provider key is keys.env's own declaration (a fact), never inferred from a `_KEY`/`_TOKEN` suffix,
  so an app secret like `CSRF_TOKEN` is never mis-flagged. New `config.dotenv_key_shadow_report` /
  `config._dotenv_scan_files` (reusing `config._iter_env_file`); `config.key_shadow_report` (runtime os.environ) is
  unchanged. No change to any existing command.

Reliability — ends the Anthropic concurrent-connection **429 storms** that had made batch/fan runs unreliable since
2026-09-28. The 429s were a concurrent-CONNECTION cap ("Number of concurrent connections has exceeded your rate limit"),
not tokens/min or requests/min — so they carry no `x-ratelimit` header, the rate learner had nothing to learn, the
per-vendor cap never ratcheted, and the api-path 429 was never retried (`attempts`/`retry_after` were NULL on all
recorded 429s). No public API change; callers do not need to set `metered_only` or retry params to get reliable fan-out.

### Fixed
- **One unified TCP-style congestion window per vendor (`dispatch.Governor`).** `_conn_admit` / `_conn_release` gate
  admission against the LIVE learned per-vendor connection cap (a `Condition` + counter, read per-admit), so a
  shrink/grow takes effect immediately WITHOUT rebuilding the bucket semaphore — the old rebuild orphaned over-admitted
  holders, kept in-flight high, and left the storm unending (the real bug).
- **AIMD on any 429/529 — no 429 subtyping.** `adapters._call_guarded` shrinks the window on any rate-limit/overload
  (multiplicative-decrease toward in-flight−1, debounced per wave) and grows it on a clean success (additive-increase
  toward the configured default). Classifying connection-vs-token would be a meaning decision, so it is not done; tpm/rpm
  pacing still rides the header independently when present.
- **Bounded re-admit retry on every path.** The fan (`lane_balance._run_task_on_api`) and serial (`adapters.call`,
  guarded by `dispatch.holding()` so it never double-retries inside a fan's slot) both re-admit under the tightened
  window until served or the caller's deadline; a deadline-spent miss is signaled, never silent; base-fallback is
  skipped for any 429.
- **Fail-closed handlers.** The window's shrink / grow / reset / forget re-raise deliberate stops before swallowing a
  transient; `reset_connection_window` is a durable empty-rewrite of the learned limits, never a file delete.
- **Window bookkeeping is safe for unregistered models.** `_call_guarded` resolves the vendor once in a guarded lookup
  (None when the model has no provider, e.g. a test `fake-model`) and skips the window update — `provider_for()` no
  longer raises mid-call.

### Tests
- New `tests/test_connection_storm_reliability.py` — burst fan-out at concurrency against a stub enforcing a connection
  ceiling (the load-induced-storm test that was missing; the prior queue test drives self-clearing faults and
  structurally cannot reproduce a storm). RED before (client-visible failures) → GREEN after (0 client failures, the
  window converges to the ceiling, bounded re-admits).

## [0.12.2] — 2026-10-01

Dependency-resolution hardening — `pip install llm-spendguard[openai|anthropic]` can no longer resolve into a broken set.
No runtime code change; the public API and behavior are identical to 0.12.1.

### Fixed
- **Pin `pydantic>=2` on the `[openai]`, `[anthropic]`, and `[all]` extras.** The openai (>=1) and anthropic SDKs are
  pydantic-2-based, but a loose/backtracking pip resolve could pull pydantic 1.x into the install — which breaks
  `import anthropic` at import time (its `httpx2.Timeout` is a pydantic dataclass; under pydantic 1.x the
  `Timeout(timeout=600, connect=5.0)` call raises *"must either include a default, or set all four parameters
  explicitly"*). spendguard imports no pydantic itself — the floor just stops the SDKs' own requirement from being
  silently downgraded. Also pinned in the `ci` + `release` workflow install steps so both resolve deterministically.
- **`test_version_dunder`'s pre-3.11 fallback no longer false-fails.** The `tomllib`-absent path string-scanned
  `pyproject.toml` and read the `[tool.setuptools.dynamic]` `version = {attr=…}` derivation line as a static
  `[project]` literal — reddening ci on 3.9/3.10 while 3.11/3.12 (and the 3.12 release gate) passed. It now parses the
  TOML structurally on every version (`tomllib` → `tomli` backport), with a corrected mechanical check only if neither
  parser exists.

## [0.12.1] — 2026-10-01

Batch cost-ESTIMATE basis fix. The estimate — the number that AUTHORIZES spend — no longer uses the model's output
ceiling × request count (~100x over), which made a $ cap unusable (a $126 job demanded an ~$11,823 cap) and trained
fail-open bypasses. What we SEND stays the ceiling (billed on ACTUAL tokens); what we ESTIMATE is what the job will
PLAUSIBLY emit.

### Fixed
- **Batch output-token estimate used the ceiling and labelled it the lying 'caller-cap'.** Now a measured/declared/
  ceiling basis, named honestly in `out_basis` (`expected_output.batch_output_estimate`), on BOTH providers
  (gate._estimate_anthropic_requests + _estimate_openai_jsonl + submit.estimate_jsonl_cost):
  - MEASURED — the per-intent learned p90 → the model-wide measured p90 (`measured:<intent>` / `measured:model`). No
    caller input; improves as the ledger grows.
  - DECLARED — a new `expected_out_tokens` (per request) on `submit_message_batch`, threaded to the gate's at-create
    estimate via the recording context; labelled `declared`, it beats the broad model-history (intent-specific) and lets
    a cold-intent job authorize without the ceiling's ~100x. `max_out` is no longer silently dropped — a passed max_out
    is pointed at `expected_out_tokens`.
  - CEILING — only the last-resort basis for a truly cold class, labelled `ceiling` (never the old `caller-cap` lie).
- **The ceiling is kept as a SEPARATE worst-case guard**, not the primary cap. The per-batch cap now bounds the realistic
  estimate; a distinct, much-higher worst-case cap (`gate._worst_case_check`; `SPENDGUARD_WORST_CASE_CAP`, default
  max(GATE_CAP × 100, $10k)) still refuses a genuinely runaway request set. Two thresholds, not one guard with the wrong
  number.
- **A cap refusal names BOTH caps + which bound.** `submit_message_batch` states the caller `cap_dollars` AND the global
  GATE_CAP and which binds — a pass here can no longer be followed by a surprise gate refusal citing the other.
- **The provisional cost row booked at submit is now the realistic estimate** (not the ceiling), so an uncollected/
  expired batch can't leave a ~100x overstatement in the ledger; $-truth is reconciled to provider billing as before,
  and `collect_message_batch` records the REAL per-result usage (both token axes).
- **EstimateNotGrounded is now a deliberate-stop type** (`gate.deliberate_stop_types()`), so a fail-open estimate handler
  propagates a refusal-to-ground instead of swallowing it into an uncapped 'allow'.

Guard: tests/test_batch_estimate_basis.py (measured / cold-ceiling / declared basis + honest labels, the worst-case
guard, the dual-cap message, the realistic provisional, max_out guidance). Full offline suite green.

## [0.12.0] — 2026-10-01

Anthropic Message Batch path + the collective data-plane's client overlay. spendguard's batch surface now spans BOTH
providers (OpenAI chat batches + Anthropic Message Batches), and a curated catalog fact the server carries reaches an
install without a package release.

### Added
- **Anthropic Message Batches — submit + collect + exactly-once offload** (the Messages-API twin of the OpenAI chat
  batch). `submit.submit_message_batch(tasks, model, …)` builds the INLINE requests through the one
  `models.apply_call_params` authority and submits via `client.messages.batches.create`, which the gate already
  intercepts (`_gate_anthropic`) for the global/daily/monthly + per-batch caps and the provisional, intent-attributed
  cost row — so there is no second, looser chokepoint. `callio.collect_message_batch(batch_ids, intent, model)` is the
  settle twin, keyed by custom_id (joined text, or a forced-tool/schema result's `tool_use.input` as JSON), recording
  REAL usage on BOTH token axes (fresh + cache_read, matching the realtime convention). REFUSE-NEVER-DEGRADE: over the
  cap it returns {error, estimate} and a gate refusal propagates — it never silently falls back to realtime.
- **Provider-aware batch offload + collect.** `batch_tracker.submit_offload` + `lane_queue.collect_batched` derive the
  provider from `batch_model` and route Anthropic through the Messages Batch path. Anthropic batches carry NO
  server-side metadata (unlike OpenAI), so exactly-once rides each request's globally-unique custom_id + a LOCAL pending
  record (written before the paid create, confirmed after); a crash-after-accept is recovered by scanning the provider
  for a batch carrying these rows' custom_ids, and an unconfirmable in-flight create is HELD (never a double-pay) — keyed
  to our own record, bounded by the completion window, and never blocking on an unrelated batch. Fail-closed throughout
  (an unreadable pending record refuses rather than blind-submits). Guards: tests/test_message_batch_{submit,collect}.py,
  test_offload_exactly_once_anthropic.py, test_collect_batched_dispatch.py (offline, shared tests/_fake_anthropic.py).
- **Client data-plane overlay (T2).** `model_catalog._load_records` layers the shipped floor → a server-synced
  `catalog_synced.json` → local overrides (content-keyed, never stale on a restore/rsync/touch), and
  `saas.sync_catalog_overlay` (wired into `spendguard sync-catalog`, fail-open) pulls the server's spendguard-CURATED
  catalog so a measured fact (an embed ceiling, a verified price) reaches this install with no package release. See
  docs/DATA_PLANE.md. Guards: tests/test_catalog_overlay*.py.

### Fixed
- **Two receipt/codex suite tests were calendar-fragile at the UTC month boundary.** They mixed local `date.today()`
  with the receipt's UTC windows (`_utc_today`), so they failed only on a month/week-boundary day — not a code bug (the
  receipt windows in UTC to match how providers bill, and keeps real-$ and est-value separate). Now UTC-consistent and
  robust to window coincidence.

## [0.11.4] — 2026-09-30

Packaging fix — ship the curated catalog so 0.11.3's embedding batch clamp actually works on a `pip install`.

### Fixed
- **`model_catalog.json` (the curated catalog SSOT) now ships in the wheel.** It was never in `package-data` (only
  `prices.json` + `py.typed`), so on a pip install `model_catalog._load_records()` found no catalog and the curated
  accessors — `embed_batch_ceiling`, `embedding_models`, curated capabilities/context/reasoning — silently degraded to
  the litellm breadth / defaults. Concretely the 0.11.3 per-provider embedding batch **clamp was INERT for pip users**
  (`embed_batch_ceiling` → None), and since litellm carries no embedding batch-cap data there was no fallback. Now
  shipped, so a pip install matches the tested editable/dev config. Pricing is unchanged — `prices.json` (the generated
  projection) already shipped and agrees with the catalog. Guard: `tests/test_packaging_data_files.py` (every runtime
  data file in the package dir must be declared in `package-data`).

## [0.11.3] — 2026-09-30

Correctness fix — per-provider embedding batch ceilings (a provider-enforced limit is now the catalog SSOT, not one
global literal shared across vendors).

### Fixed
- **`adapters.embed()` applied ONE global batch size to every OpenAI-compatible embedding provider.** Gemini's
  `batchEmbedContents` caps at 100 inputs/request, and because a too-large batch 400s the WHOLE chunk, a 5,768-text
  run on `gemini-embedding-001` returned **5,760/5,768 unembedded**. The ceiling is now a catalog SSOT
  (`capabilities.embed_max_batch`, MEASURED by live bisection 2026-09-30 — gemini=100, OpenAI text-embedding-3-*=2048,
  voyage-3.5=1000) read via `model_catalog.embed_batch_ceiling()`, and `embed()` CLAMPS every chunk to
  `min(requested, ceiling)` so a caller cannot exceed it. For an UNCURATED provider, a still-rejected batch is
  BISECTED empirically (the provider's accept/reject is the oracle — no error text is parsed) until the inputs fit,
  so all inputs embed instead of all failing — no double-pay (the rejected request billed nothing). Guard:
  `tests/test_embed_batch_ceiling.py`; the
  measurement tool: `scripts/reliability/embed_ceiling_probe.py`; the postmortem: `docs/INCIDENTS.md`.

## [0.11.2] — 2026-09-30

Onboarding release — make `spendguard init` a true one-command setup: a new user is wired into their whole stack in
one step, their $0 subscription lanes are detected and used automatically, and a first-timer is never left guessing
where a key comes from. No breaking changes; plain `spendguard init --quick` stays config-only.

### Added
- **`spendguard init` auto-detects your $0 subscription lanes** and, when at least one CLI is installed + logged in,
  sets `advisor.executor = pool` so realtime meta/best-value calls prefer your plans (billed $0) before the metered
  API. Never clobbers an executor you — or `$SPENDGUARD_ADVISOR_EXECUTOR` — already chose; no ready lane ⇒ no change
  (safe on CI). Intentionally does NOT auto-seed `advisor.lane_models`: without the plan's real model a priced guess
  would undercount lane value, so you declare it via `spendguard lanes set-model` (which validates it is priced).
- **`spendguard init --all`** folds the five installers into one guided flow — gate hook · MCP tools · in-chat
  receipt · global assistant rule · slash-commands — each a per-step [Y/n]; `--all --quick` runs them all zero-prompt.
  A governance stop (spend refusal / ledger lock) from any step propagates, never downgraded to "skipped".
- **Key pre-flight over EVERY declared provider/compute key** (not just openai/anthropic), with a get-a-key link for
  each one not set — from a new `config_schema.KEY_HELP_URLS` table (schema-driven, not a literal in setup logic).
- **A closing setup card** — gate enforcing here, keys resolved, ready $0 lanes + `advisor.executor`, what was
  wired — then the one `$0` command that proves the path end-to-end (`spendguard lanes --probe`), offered never
  auto-run (a probe bills plan tokens; setup must not spend for you).
- Discoverability: `spendguard --help` and the first-run nudge lead with the quickstart
  (`pip install llm-spendguard[all] && spendguard init --all`); the README gains a one-command setup block.

## [0.11.1] — 2026-09-30

Reliability + correctness release — ledger-truth, schema robustness, and embedding fixes that public consumers
(`llm-spendguard[openai,anthropic]`, e.g. via honestreview) need. 26 commits since 0.11.0.

### Fixed
- **Token-count integrity — a metered call no longer records a FABRICATED ceiling estimate.** A call routed through
  `chat.completions.with_raw_response.create` (used to read the vendor's rate-limit headers) returns a wrapper with no
  `.usage`; the usage extractor read `None` and the recorder fell back to the model's published OUTPUT CEILING as the
  token count — recording e.g. a 35/16-token deepseek reply as `in=5 / out=393,216 / $0.47`, a ~9,000× over-record on
  EVERY OpenAI-compatible call, poisoning the ledger, cost advice, and estimates. The chat/responses/anthropic usage
  extractors now reach through the raw-response wrapper via `.parse()` (cached — one parse, no re-read).
- **gemini / voyage embeddings crashed** with `int(None)` when a provider returns `index=None`; now keyed by the
  stamped index when present, else the enumeration position (exact per the OpenAI-compat input-order guarantee).
- **Schema validation** — UNION types (`["number","null"]`) no longer raise (the real healiom-investor-score crash); a
  malformed contract is refused cleanly BEFORE billing (was an opaque `TypeError`, answer billed then discarded); and
  declared value constraints (enum / const / bounds / `additionalProperties`) are enforced, not just type/required.
- **The data-integrity guard is never blind** — an impossible per-call `out_tok` is flagged even for a model with no
  published ceiling (resolves output-ceiling → context-window → a universal bound).
- Realtime capture lands under the caller's INTENT (not `sig`) and honors `store_prompts`; an explicit `timeout_s` no
  longer breaks the Anthropic VISION path; `bulk_delegate`'s lane path delivers the resolved prompt, not the raw key;
  `experiment.py` routes output-budget + reasoning through the canonical homes (kills the 1500/400 hardcodes).

### Added
- **Embedding models are a CATALOG** (`model_catalog.embedding_models`) — the SSOT for "which models embed" (records
  marked `mode: embedding`); gemini-embedding-001 + voyage-3.5 added; `_embed_default_model` / `embed_compare` and the
  reliability harness all DERIVE from it — no hardcoded embedding-model ids anywhere.
- Model capabilities (vision, `response_schema`, output ceilings) sourced from the LiteLLM breadth cache with a daily
  freshness + completeness audit. `fell_from` on the calls ledger (which $0 lane a metered call fell over FROM) with a
  `spendguard lanes --fallback-spend` rollup. Batch exactly-once offload (reconcile-before-submit) + >25K auto-chunking.
  The OVERLOADED (429/529) retryable failure class covered end-to-end. A logged-out lane surfaces its exact re-login
  command. A consumer×provider SEAM smoke matrix (`scripts/reliability/consumer_provider_smoke.py`, measure-then-project).

### Changed
- Call-path lane chatter is OPERATOR-opt-in (off by default) — a consumer never sees "lane" on the call path; a down
  lane still fails over silently to its metered twin.
- Attribution evidence is sent WHOLE (no silent `[:6000]` cut; input bounded only at the provider window); GPU-relevance
  and other meaning classifications are LLM decisions, not regex gates.

## [0.11.0] — 2026-09-26

### Added
- **The whole-job contract — hand spendguard a SET of calls + a GOAL; it plans, budget-gates, runs, and returns.**
  New `whole_job.run_jobs(jobs, goal)` / `plan_jobs` / `collect_jobs`, the CLI `spendguard submit-jobs`
  (PLAN + estimate by default, $0; `--execute` runs it and REQUIRES `--budget`; `--collect` settles async batch
  handles), and the spend-safe `spendguard_run_jobs` MCP tool (no budget → plan-only, never an unbounded fan). It
  groups by intent, picks batch-vs-lane-vs-metered per group at TRUE marginal cost, capability-matches each call's
  schema to a path that can deliver the shape, enforces the budget **estimate-first + fail-closed** (an over-estimate
  OR an unpriceable group under a budget is refused, with a structured `refused_code`), and never loses paid work
  (durable batch handles, tracked persist/submit failures). The recommended way to run any cost-sensitive batch.
  `docs/WHOLE-JOB.md`, `tests/test_whole_job.py`.
- **Capability-aware auto-route — a strict schema goes to a path that can ENFORCE it, automatically.** A schema that
  declares `required`/`nonempty` cannot be guaranteed by a prompt-only subscription lane (a CLI can only ask, and
  wraps its JSON in prose); spendguard now routes such a call to the vendor's metered path (Anthropic forced-tool /
  OpenAI strict `response_format`; every OpenAI-compatible vendor's `json_object`) instead of churning on the lane and
  falling back reactively. Lenient schemas still ride the $0 lanes. `adapters.schema_capability` /
  `output_contract.needs_enforcement`, `tests/test_capability_auto_route.py`.
- **The team roll-up push now carries TOKENS and the per-intent efficiency signal** — the two dimensions the org
  dashboard could use but wasn't receiving. `saas` roll-up rows now include `in_tokens`/`out_tokens`/
  `cached_in_tokens` (COUNTS only, no content — feeds unit economics: $/token, cache-hit rate), and `spendguard saas
  sync` now also pushes the per (project·intent·model) signal (cost + quality $/good-result + waste), so spend
  "by intent" (the WHAT-KIND axis) flows on the normal cadence instead of only via a separate command.
  `ledger.sum_by` gains a token `int_cols` sum; `tests/test_saas_payload.py`.

### Changed
- **A DOWN lane always fails over — even under `--refuse-billed` / `no_metered_fallback`.** A lane whose executor
  errored (login/token expired, CLI crash, rejected model) is INFRASTRUCTURE failure, not a task the free lane found
  too hard — so it now applies the ladder (reroute to another $0 lane, then the metered twin) and surfaces the exact
  re-login step, rather than silently returning an empty. `no_metered_fallback` still suppresses metered for a genuine
  TASK miss (empty/off-shape); `budget_usd` remains the hard $0 cap for a caller that must never bill. Closes the
  "a logged-out lane silently dropped work" gap. `tests/test_lane_down_overrides_refuse_billed.py`.

### Fixed
- **Ledger reconcile — real metered spend the gate recorded but never wrote to the money ledger is now booked.**
  `reconcile_calls` fills the money-of-record from local `calls` telemetry for RECORDING_GAP cells, sized against what
  `spent_dec` already counts (so an `estimate` row already standing in for the spend is never double-counted), booked
  as `billed`, reversible (`source='reconcile-calls'`), idempotent. Impossible per-call output-token counts are
  flagged `suspect` and excluded rather than trusted. `decisions.why` records the specific routing reason for
  post-hoc analysis. `scripts/diag/reconcile_divergence_diagnose.py`, `src/spendguard/reconcile_calls.py`.
- **`adapters.call(governed=…)` was reject-flagged BEFORE it was popped — the documented governor kwarg was
  unreachable.** The ensure-success unknown-kwarg reject loop ran before the `aliases.pop("governed")` at the
  dispatch-governor step, so `call(..., governed=True)` (the concurrent-fan path) AND every `call(..., governed=False)`
  single call raised `TypeError: got an unexpected keyword 'governed'`. That broke every consumer forwarding
  `governed=` — e.g. warden's `spendrails.call`, i.e. ALL its describe/classify/preflight/doctrine-review calls — a
  break the hermetic mocked tests never exercised. Fix: skip `governed` (a real feature kwarg carried via `**aliases`,
  honored at the governor step) in the reject loop. Guard: `test_call_normalises_caller_kwargs.py` now asserts
  `call(governed=False)` does not raise while an unknown kwarg (e.g. `temperature=`) still fails loudly.

### Added
- **Canonical lane↔metered REASONING-EQUIVALENCE map — the atomic (lane, metered) pair is now PROVABLE, not hoped.**
  New `src/spendguard/reasoning_equivalence.py`: for every lane (Claude/codex/agy-Gemini/zai) × model × reasoning
  level, it derives the SAME-provider metered call at the **equal model + equal-or-greater reasoning**, verifies it
  is priced+served (`availability` = yes/unverified/no), and carries a status + provenance. It unifies what was
  scattered across `models.normalize_reasoning`, `codex_exec._codex_effort`, `lane_catalog.REASONING_QUIRK` and
  `adapters.metered_fallback_id`, so "is the lane→metered fallback faithful?" can finally be READ and PROVEN. A pin
  is a **provider + a reasoning FLOOR**: fallback stays same-provider (a pinned agy/Gemini call can never fall to
  codex) at equal → bake-off-proven-lesser → round-up-to-greater; never under-reasons, never crosses vendor. A
  cheaper `proven_lesser` is trusted ONLY on an affirmative agentic verdict (`record_equivalence` refuses free-text —
  "equally good" is a MEANING judgement). Persisted (`~/.spendguard/reasoning_equivalence.json`, learnings maintained
  across re-derivations). Surfaced by `spendguard lanes --reasoning-map`. `docs/GATED_BULK_LANES.md` §4,
  `tests/test_reasoning_equivalence.py`.
- **Lane REACHABILITY probe — a declared lane model reads 🟢 only if its CLI actually ACCEPTS + SERVES it.**
  `tier_config.reachability_probe` / `cached_reachability` dispatch a tiny pinned $0 probe (`no_substitution` +
  `no_metered_fallback`, so a miss is an error row, never a metered call) through the PRODUCTION path for each enabled
  lane's declared tier model; a mapped-but-CLI-rejected id reads 🔴 (silently meters). Surfaced by
  `spendguard tiers --probe` and in `spendguard doctor`. Closes the "declared + priced + mapped, but the lane CLI
  rejects the id → silent metered fallback" gap. `tests/test_lane_model_reachability.py`.
- **Realtime surfaces the ADMIN-FREE reconstruction as an ESTIMATE in `reconcile all`, never a false "UNKNOWN".**
  `RealtimeSource` + `ledger_sync.realtime_reconstruction_estimate` present spendguard's reconstructed realtime $
  (from conversation token records, no admin key) as a clearly-labelled reconstructed **estimate**; a stale/absent
  cache reads an accurate "reconstruction stale — run the find" note instead of the misleading "bill could not be read
  (key/network)"; the completeness verdict reads `ESTIMATED (reconstructed, admin-free)`. A corrupt cache is a surfaced
  failure, not silent absence. `tests/test_realtime_reconstruction_surface.py`.

### Fixed
- **A pinned `gemini:gemini-3.8-flash` SILENTLY METERED — agy rejects the bare base id.** agy (the Gemini lane CLI)
  serves ONLY tier-suffixed ids and rejects the bare base, so a pinned base id fell through to the metered API.
  `adapters._compose_gemini_reasoning` now resolves a bare base id to the lane's DEFAULT served tier (`…-flash-medium`,
  from `lane_catalog.quirk`). `tests/test_lane_model_reachability.py`, `tests/test_gemini_reasoning_namespace.py`.
- **`spendguard reconcile` dumped a raw urllib traceback on a provider HTTP error (e.g. a 401).** The CLI caught
  `RuntimeError`, but `urllib.error.HTTPError` (an `OSError`) slipped past. `reconcile_openai.fetch_batches` now raises a
  typed `BatchFetchError(RuntimeError)` carrying the status + OpenAI's own body reason — so a 401 self-diagnoses
  (`[OpenAI: Incorrect API key provided]` = replace the key, vs `Missing scopes` = re-scope it) and the command exits
  cleanly instead of crashing. `tests/test_reconcile_openai.py`.
- **A pinned claude-haiku fallback would STRAND (the equal model wasn't callable on the metered API).** The Claude
  CLI accepts the bare alias `claude-haiku-4-5`, but the metered API serves only the dated id
  `claude-haiku-4-5-20251001` — so a lane miss had no equal-model to fall back to. `adapters.metered_fallback_id` now
  resolves a stale bare alias to its served dated variant ($0, from the served-list cache; the `-YYYYMMDD` date
  suffix is a fixed-format parse). The old `lane_catalog.audit_lane_fallback` missed this because it only checked each
  lane's single default (strong) model; the new map checks every model × level.
- **A pinned agy/Gemini call at `reasoning="minimal"` UNDER-reasoned on fallback.** The agy lane runs its default
  tier (medium) for a value it has no suffix for, but the metered fallback normalized `minimal`→`none` — less
  reasoning than the lane. `adapters._call_once`'s lane→metered fallback now routes through
  `reasoning_equivalence.resolve_metered`, guaranteeing the same-provider metered call at equal-or-greater reasoning.
- **Vision/reasoning calls truncated at ~7–28 tokens — the auto-heal had POISONED a model's learned max_output.**
  The output-budget auto-heal halves `max_completion_tokens` on a 400 and records the accepted value as the model's
  max_output. A 400 from a NON-budget cause (a malformed vision request, a transient error) ALSO drove the halving,
  and with no lower bound it recorded an absurd ceiling — MEASURED: gpt-5-mini learned max_output=**7**, so every
  call was clamped to ~7 output tokens, gpt-5-mini's hidden reasoning consumed the budget, and bulkgate showed
  **51% of the class truncating**. (A direct SDK call worked because it bypassed the clamp.) Fixed: the halving now
  stops at `_MIN_LEARNED_MAX_OUTPUT` (1024) and never learns a ceiling below it — no chat model caps output in the
  hundreds, so a sub-floor "success" is a non-budget 400, not a real limit; the poisoned gpt-5-mini fact is cleared
  (→ falls back to the 32K floor and self-heals correctly). New `models.clear_fact`; the halving is extracted to the
  unit-testable `adapters._heal_token_budget`. Guard: `tests/test_token_budget_heal_floor.py`.
- **No double-usage: a subscription LANE subprocess can no longer carry ANY provider's metered key — a non-Claude
  lane can never spend Claude tokens.** `keys.env` is loaded into `os.environ` at import, so a codex/gemini/claude-code
  subprocess inherited EVERY provider key while each lane stripped only its OWN (codex dropped `OPENAI_API_KEY` but
  still carried `ANTHROPIC_API_KEY`, etc.). New `config.lane_plan_env(keep=())` builds a child env with every metered
  key AND flat-fee plan token removed (names from the provider registry + the auth-token/base-url variants), except
  what a lane explicitly owns; all four subprocess lanes (subscription_exec, codex_exec, codex_daemon,
  antigravity_exec) now use it. Structural guarantee that a lane rides its plan login and cannot make a metered call
  of any provider. Guard: `tests/test_lane_no_double_usage.py` (incl. capturing the env the codex subprocess receives).
- **honestreview high+medium sweep across the changed files.** Fixed, each verified: `calls.enabled()` now FAILS
  CLOSED (`SPENDGUARD_CALLS=False`/`off`/typo no longer silently ENABLES prompt/output recording — privacy);
  `_link_used` matches the full stored snippet with a length floor (was an 80-char prefix that false-labelled outputs
  'used' on shared boilerplate); `tested_recently` guards empty `kinds` (was invalid `IN ()` SQL); `lane_value`
  marks its cache fresh only on a SUCCESSFUL stamp (a failed stamp no longer goes stale for `max_age_s`); the lane
  dispatch wraps `run_prompt` so a lane that raises/returns a non-dict degrades to the API instead of crashing
  `call()`, and requires real text for success; `adapters` strips the `provider:` prefix before `pricing.max_output`
  (a qualified id missed its output cap); `codex_exec._usage_from_events` descends into ARRAYS (array-nested usage no
  longer reads 0) and the module contract is honest that the warm-daemon path ESTIMATES tokens; `codex_daemon.shutdown`
  now `wait()`s + kills (no zombie children on restart); `cli` passes `--since` to `reconcile all` (was dropped),
  guards `quarantine --since` with no value (was an uncaught IndexError), and warns instead of silently swallowing a
  failed `gate.install()`; `receipt` closes the gate-blocks file handle, and `_all_repos` reads the est-value `cells`'
  team/project (it read a `repos` key that is never written, so plan-value-only repos vanished from `--all`); and
  removing the Claude Code receipt hook now strips only OUR hook, not the whole Stop group (no deleting a user's
  unrelated hooks). Guards updated/added in `test_codex_daemon.py`, `test_receipt_repo_est_scope.py`,
  `test_cli_reconcile_dispatch.py`.
- **Per-repo receipts showed the GLOBAL plan value under every repo (honestreview HIGH).** `tally(project=repo)`
  scoped the billed API-$ to the repo but `est_value` was always computed globally, so each per-repo line showed its
  own spend beside the WHOLE plan total — and `_sum_repos` (the "+ N more repos" summary) added that global est once
  PER repo, an N× over-count. `_est_tally` now takes a `repo` scope (a cell counts if its TEAM or its PROJECT equals
  the repo — the same mapping the per-repo project breakdown already uses), and `tally(project=…)` passes it. The
  global tally stays global; a repo with no classified plan value now honestly shows $0 instead of the global total.
  Guard: `tests/test_receipt_repo_est_scope.py`.
- **Warm Codex daemon hardened (honestreview 5-vendor pass on the diff).** Multi-vendor review found three real
  robustness gaps in the `codex mcp-server` client: (a) the spawn handshake's `_mcp_send` sat outside any try/except,
  so a server dying between `Popen` and the initialize write raised `BrokenPipeError` out of `ensure_running()` and
  **crashed the caller** — the whole handshake is now guarded (→ None, a startup failure the lane degrades on);
  (b) `_read_until` used blocking `readline()` after `select()`, which could **hang past its deadline** on a partial
  line the server wrote then stalled on — while holding the shared lock, wedging the lane — now non-blocking `os.read`
  on a binary pipe with our own line-splitting, so the deadline is always honored (and a line buffered below `select`
  can't be missed); (c) `_spawn` matched the initialize reply against the GLOBAL `_rpc_id` instead of the captured id.
  Also `codex_exec.run_prompt` now wraps the `run_warm` call so a daemon EXCEPTION can never bypass the exec/API
  fallback. Guards: `tests/test_codex_daemon.py` (real-pipe deadline + spawn-never-raises + exception-safety).
- **Per-flow receipt could report a $0 subscription flow as billed spend.** `emit_flow` used `actual if actual else
  cost`, so a MEASURED $0 (a plan-served flow) fell back to the ESTIMATED cost — showing plan-covered work as real
  money. Now `actual if actual is not None else cost`: a measured $0 is the truth; the estimate is used only when
  actual could not be measured at all.
- **Codex lane recorded a rejection (400) as a $0 SUCCESS — the error body was handed downstream as content.**
  A `codex mcp-server` tools/call result carries `isError: true` when the tool fails (e.g. the plan rejecting a model:
  "gpt-5-mini is not supported when using Codex with a ChatGPT account"), but `codex_daemon._extract`/`run_warm`
  ignored the flag and returned the error text as `text` — so spendguard recorded a $0 `subscription` "success" and
  the caller got the error prose instead of an answer (surfacing downstream as "missing/empty chunks"). Now
  `run_warm` checks `isError` → `{error, tool_error}`; `codex_exec.run_prompt` propagates a `tool_error` as an error
  (the adapter falls back to the metered API) instead of cold-retrying a `codex exec` that hits the same 400.
- **A subscription lane re-intercepted a model it can't serve on EVERY call.** `_learn_from_fallback` learned only a
  SIZE ceiling, so a model rejection (within a proven-good prompt size, where the API then answers) was classed
  "unsuitable" and the lane kept grabbing that model forever. Added a per-`(lane, model)` backoff (`_lane_model_cool`):
  a model-specific rejection cools THAT model on THAT lane (self-healing, expires), while the lane stays available for
  the models it does serve (gpt-5.5 keeps riding codex). Decision is the API-fallback OUTCOME, never the error text.
  Guards: `tests/test_codex_daemon.py` (isError round-trip), `tests/test_lane_model_backoff.py`.
  NB: `SPENDGUARD_ADVISOR_EXECUTOR=api` (force the metered API, no lanes) already works — verified — but is per-process
  env; the cross-process persistent switch is `spendguard config set advisor.executor api`.
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

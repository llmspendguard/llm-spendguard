# Changelog

All notable changes to **llm-spendguard**. Format loosely follows Keep a Changelog; dates are UTC.

## [Unreleased]

### Testing
- **Coverage pass on the money-critical core + a scoped CI gate.** New offline tests for `tag.py` (attribution
  cascade, 0→100%), `guard.py` (the guarded-spend lognormal cumulants, 43→100%), `signal.py` (efficiency roll-up,
  0→49%), `pricing.py` (now also `freshness`/`providers`/`_load`/`main`, 54→75%), `reconcile.py` (`all_sources`/
  `report`/base `Source`, 61→92%), and `gate.py` (`realtime_by_day` + the CLI surface, 56→67%). CI now enforces
  **two floors**: a whole-package regression floor (40%) AND a **78% floor on the money-critical core** (gate,
  ledger, reconcile, pricing, attribution, …) — today 81%. The package number is held lower on purpose: I/O-adapter
  modules (chat→claude.ai, saas push, transcript parsers, paid-call tools) are integration-tested, not unit-tested.

### Added
- **Full OpenAI + Codex parity — accounting works the same for both providers and both coding agents.** New
  `codex.py` (+ `spendguard codex show|classify|sync`) mines `~/.codex/sessions/**` into est-value (channel=codex,
  billed=false — Codex on a ChatGPT/Codex plan is plan-covered, exactly like Claude Code), classified
  org→team×project and **summed into the same receipt/tally** as Claude Code + claude.ai (per-source, never
  clobbering). The token total comes from the cumulative `token_count` events; the model from `turn_context`. The
  gate now also intercepts the OpenAI **Responses API** (`client.responses.create`, sync + async) — previously only
  Chat Completions was gated, so modern OpenAI realtime (incl. Codex-style `responses` calls) was an un-gated
  actual-$ gap; now estimated pre-call + recorded post-call (incl. `input_tokens_details.cached_tokens`) like every
  other surface.
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

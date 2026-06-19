# Changelog

All notable changes to **llm-spendguard**. Format loosely follows Keep a Changelog; dates are UTC.

## [Unreleased]

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

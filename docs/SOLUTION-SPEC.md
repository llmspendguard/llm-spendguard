# llm-spendguard — Solution Specification (client)

> The single authoritative document for the **client**: why it exists, the value it delivers, how a dollar of LLM
> spend flows through it, the principles that make it work, the complete design, and how it's tested, secured,
> operated, and extended. It is the umbrella over the focused docs ([ARCHITECTURE](ARCHITECTURE.md),
> [learning-advisor](learning-advisor.md), [work-attribution](work-attribution.md), the [README](https://github.com/llmspendguard/llm-spendguard/blob/main/README.md)
> command reference, [SECURITY](https://github.com/llmspendguard/llm-spendguard/blob/main/SECURITY.md)) — read this for the *whole story*; follow the links for depth. The
> server half of the system has its own [Solution Specification](https://github.com/llmspendguard/llm-spendguard-server/blob/main/docs/SOLUTION-SPEC.md).

**Status:** continuously developed; pip-published. **Audience:** an engineer or technical buyer who needs to
understand *what this is, why it's built this way, and whether it's trustworthy*. **One line:** a zero-dependency,
on-device cost-governance layer for OpenAI/Anthropic that **gates** spend before it happens, **attributes** it to
the right project, **reconciles** it against provider billing so the numbers actually add up, and **recommends**
how to spend less — without your prompts or keys ever leaving the machine.

---

## 1. Executive summary

Teams building with LLMs routinely discover, weeks later, that a background job, a mis-set `max_tokens`, or an
ungoverned script burned far more than expected — and they can't say *which project* or *which person* it was, or
whether a "cheap" run was actually cheap. The spend is invisible until the invoice, and the invoice doesn't
decompose.

`llm-spendguard` makes LLM cost a **first-class, governed, attributable** quantity, on the machine where the spend
originates:

- **Gate** — it intercepts the OpenAI/Anthropic SDK calls, estimates cost *before* the call, and enforces caps
  (per-batch, per-day, cumulative real-time). Fail-closed where it matters (a paid batch over cap is refused),
  fail-open where safety demands it (a bug in the gate must never break your call path).
- **Account** — every charge lands in a local SQLite ledger, tagged to a project via a free deterministic cascade.
- **Reconcile** — it reads the providers' *actual billing* (read-only) and proves the local ledger against it
  through one account-anchored loop, surfacing any unattributed remainder instead of hiding it.
- **Advise** — a *caged* advisor (its own meta-budget, estimate-first) turns your own usage corpus into "which work
  is worth the spend" and "how to spend less."
- **Share (optional)** — it can push *scrubbed aggregates* (never prompts, never keys) to the
  [SaaS server](https://github.com/llmspendguard/llm-spendguard-server) for an org-wide roll-up.

It has **zero required runtime dependencies**, runs entirely on-device, and is governed by a hard rule the
maintainer applies to their own work: *no LLM code runs ungated*.

## 2. The problem & why it exists

LLM spend has three properties that defeat ordinary cost tracking:

1. **It's pre-incurred and irreversible.** By the time a batch finishes, you've paid — you can't cancel your way
   out (a cancelled batch still bills the completed requests). Control has to happen *before* the call.
2. **It's mis-estimated by default.** Prices change and get hardcoded wrong. A real incident: `gpt-5.5` batch was
   hardcoded as `(1.25, 10.0)` in ~10 scripts when the true rate was `(2.50, 15.00)` — every "est ~$X" was 3-4×
   too low, which is exactly why cost-conscious days still produced $200+ charges. (`pricing.py` exists *because*
   of this.)
3. **It doesn't decompose.** The provider invoice is one number. It doesn't tell you which project, which
   teammate, which intent, or which work was even worth it.

spendguard is the answer to "*make LLM spend something I can see, cap, attribute, and trust — before the invoice,
on my own machine, without shipping my prompts anywhere.*"

## 3. The value

| For | Value |
|---|---|
| **The individual engineer** | A hard cap so a runaway job can't surprise you; a pre-flight `$` estimate; a clean per-project P&L with no manual bookkeeping; "spend less" recommendations from your own history. |
| **The team/org** (with the optional server) | Org → team × project roll-ups; coverage ("is every seat actually enforcing?"); shared, scrubbed learnings; seat-based billing that derives from real usage. |
| **The security-conscious buyer** | Prompts and keys never leave the device; the server only ever sees scrubbed aggregates; fail-closed enforcement; a published [threat model](https://github.com/llmspendguard/llm-spendguard-server/blob/main/docs/THREAT-MODEL.md). |

The quantified upside is twofold: **avoided overspend** (caps + estimate-first stop the $200-surprise class of bug)
and **realized savings** (cache/cascade/advisor), both measured — see [§8](#8-reconciliation-proof-it-adds-up) and
`guard.py`'s savings distribution.

## 4. Solution overview

The client is a Python package (`spendguard`) that installs a **gate** into the running interpreter and exposes a
CLI. The pieces, by role:

- **Enforce / record:** `gate.py` (SDK interceptors + decide/record), `budget.py` (the SQLite ledger + caps),
  `guard.py` (quantify guarded spend).
- **Attribute:** `tag.py` (the project cascade), `attribution.py` (the shared org→team×project classifier).
- **Reconcile:** `reconcile.py` (the one loop + `Source` adapters), `ledger_sync.py` (LLM source),
  `resources.py` (GPU/vast source), `reconcile_openai.py` / `reconcile_anthropic.py` (provider billing readers),
  `pricing.py` (canonical prices).
- **Advise / learn:** `advise.py`, `advisor.py`, `experiment.py`, `cascade.py`, `semcache.py`, `cacheaudit.py`.
- **Share:** `saas.py` (the `/v1` push), `schedule.py` (the cross-platform scheduler), `signal.py` (efficiency
  signal), `workdone.py` (what got done).
- **Surfaces:** `cli.py` (commands), `chat.py` (the opt-in claude.ai value adapter).

See [ARCHITECTURE.md](ARCHITECTURE.md) for the module-by-module map.

## 5. The journey of a dollar (client half)

This is the spine — follow one dollar of spend from intent to org roll-up. (The server half picks up at step 6; see
the [server spec](https://github.com/llmspendguard/llm-spendguard-server/blob/main/docs/SOLUTION-SPEC.md).)

1. **Your code calls the SDK.** `client.batches.create(...)` or `chat.completions.create(...)`. The gate has
   monkey-patched the SDK method (`gate.register` / the interceptor registry), so the call is intercepted first.
2. **Estimate before spend.** The gate reads the request, counts tokens, and prices it via `pricing.py` (canonical
   rates — never a hardcoded guess). For batches it parses the JSONL/requests to estimate the whole job.
3. **Decide.** `_decide` checks the estimate against the caps — per-batch cap, daily/monthly caps, and the
   real-time cumulative budget. Over cap → `SpendGateRefused` (fail-**closed**: the paid call does not happen). A
   bug *in the gate itself* → fail-**open** (your call proceeds; governance must never break the call path).
4. **Record.** The actual cost is written to the local SQLite ledger (`budget.record`) tagged with provider, model,
   kind (batch/realtime/meta), and a **project** resolved by `tag.py`'s free deterministic cascade (repo/cwd/config;
   `meta` → spendguard's own `llm-spendguard`). Guarded savings (a cache hit, a blocked call, a cascade downgrade) are
   recorded by `guard.py` as a lognormal distribution (cumulants that add).
5. **Reconcile against truth.** On demand (or on schedule), `reconcile.py` reads each provider's *actual billing*
   (read-only) as `truth_total`, sums what the gate `captured`, and computes `residual = truth − captured −
   attributed`. The unattributed remainder is **surfaced**, never dumped on a project. This is the
   "[does it add up](#8-reconciliation-proof-it-adds-up)" guarantee.
6. **Push (optional).** `saas.py` sends *scrubbed aggregates* over HTTPS with a Bearer ingest key — `(scope,
   member, project, day, provider, model, spend, tokens)` and scrubbed insight abstracts. **No prompts, no keys,
   no PII.** `schedule.py` can run this daily/hourly via the OS-native scheduler. → the server takes over.

## 6. Key concepts & principles

- **Fail-closed for money, fail-open for the call path.** A paid action over cap is refused; a defect in the gate
  must degrade to "your call still works, just ungoverned" — never to a broken call. `require()` inverts this for
  scripts that *demand* governance: it raises if the gate isn't actually enforcing in this interpreter.
- **Estimate-first (the API spend protocol).** Any paid batch does a separate **zero-spend** estimate (count + `$`),
  which must be confirmed before submission. Never cancel a running job as cost control — completed requests bill.
- **Account-anchored reconciliation.** Magnitude comes from *billed truth + captured*; the agentic layer only
  decides *attribution* (who/what), never *how much*; and only the **account-owner** reconciles a shared account's
  gap (so a non-owner can't claim another tenant's spend). See [§8](#8-reconciliation-proof-it-adds-up).
- **The caged advisor.** spendguard's *own* LLM use (recommendations, classification) runs under a separate
  **meta-budget** (`caps.meta`, default $2/day) and is estimate-first — the tool can't overspend while telling you
  to spend less.
- **Never hardcode a price.** `pricing.py` is the single source of truth; a CI audit fails the build on a mispriced
  literal anywhere in the source.
- **No hardcoded identity.** Project/attribution logic is driven by the user's own taxonomy + config, not baked-in
  names — the package ships generic.
- **On-device by default.** The sensitive things (keys, prompts, outputs) never leave; sharing is opt-in and
  scrubbed.

## 7. Architecture

Zero required runtime dependencies (the OpenAI/Anthropic SDKs and `tiktoken` are *optional* — the gate fails open
if a SDK is absent). The gate installs by monkey-patching SDK methods through a small **interceptor registry**:
each entry is `(module, class, method, gate_fn, is_async)`; adding a provider/surface is one entry + a `gate_fn`,
no other code changes. State is a single SQLite ledger under `~/.spendguard/` (configurable via `SPENDGUARD_HOME`),
which makes the gate **cross-process** (a fleet of workers shares one cap). The kill switch (`GATE_DISABLE=1` or a
flag file) and `require()` give explicit control of enforcement. Full module map: [ARCHITECTURE.md](ARCHITECTURE.md).

## 8. Reconciliation: proof it adds up

The reconcile core (`reconcile.py`) is one loop shared by every spend source via a `Source` adapter (LLM batches +
realtime, GPU/vast, and future subscription/storage). For each source:

```
gap        = truth_total − Σ captured          (truth = the provider/account's ACTUAL bill, read-only)
attributed = attribute_gap(gap)                (AGENTIC, account-owner only — who/what, from evidence)
residual   = truth − Σ captured − Σ attributed → SURFACED, never hidden
```

Three honesty properties are enforced and **tested end-to-end** (`tests/test_reconcile_e2e.py`, the "copy-then-
trim-by-date" suite): trim exactness + monotonicity + pivot-closure on the real ledger; a residual that stays
constant under every date cutoff; and **the portfolio reconciles** — `Σtruth − Σcaptured − Σattributed = Σresidual`
across all sources. If a provider's bill can't be read, `truth` is **`None` (UNKNOWN)** and the residual is `None`
with a loud warning — a failed fetch never masquerades as "$0 / 100% covered." See [work-attribution.md](work-attribution.md).

## 9. Testing & quality strategy

- **Offline + deterministic.** Every test runs with no network and no spend (SDKs stubbed; provider readers
  stubbed). Each test file runs as an isolated subprocess in its own `SPENDGUARD_HOME` (`tests/test_runner.py`), so
  nothing touches your real `~/.spendguard`.
- **Scoped coverage gate.** CI enforces **two floors**: a whole-package regression floor (40%) and a **78% floor on
  the money-critical core** (gate, ledger, reconcile, pricing, attribution — today **81%**). The package number is
  held lower *on purpose*: I/O-adapter modules (chat→claude.ai, the SaaS push, transcript parsers, paid-call tools)
  are integration-tested, not unit-tested, and chasing their line coverage would be theatre. We test where money
  correctness lives.
- **Price-literal audit.** A CI step fails the build if any source file hardcodes a price that disagrees with
  `pricing.py` — the founding bug class can't regress.
- **Lint.** `ruff` (pyflakes + bugbear) over `src` and `tests`.

## 10. Security

Prompts, outputs, and provider keys **never leave the device**; the optional push is scrubbed aggregates only and
refuses any non-HTTPS URL. The claude.ai chat adapter (opt-in) decrypts its session key in-process (never on argv,
never logged). The caged advisor can't overspend. Full surface + disclosure policy: [SECURITY.md](https://github.com/llmspendguard/llm-spendguard/blob/main/SECURITY.md);
system threat model: [THREAT-MODEL.md](https://github.com/llmspendguard/llm-spendguard-server/blob/main/docs/THREAT-MODEL.md).

## 11. Operations

- **Install the gate:** `spendguard install-hook --venv <v>` (or `--user`), then `spendguard doctor` — it prints
  `ENFORCING HERE: YES/NO` so a bypass is visible.
- **Schedule:** `spendguard schedule [--daily]` wires the OS-native scheduler (macOS launchd / Linux crontab /
  Windows schtasks) to run `saas sync --if-due` — snapshot GPU every run, push the roll-up when due. Idempotent +
  removable, zero deps; credentials resolve from home-based config so it works in a minimal cron environment.
- **Control:** `GATE_DISABLE=1` / `spendguard off` (kill switch); `GATE_CAP` and per-class caps via env/config.

## 12. Extensibility

- **A new SDK/surface:** write a `gate_fn` and add one interceptor-registry entry (or call `register(...)`).
- **A new spend source for reconcile:** implement a `Source` adapter (`truth_total` / `captured` / `attribute_gap`
  / `conn`) and it flows through the same `run()` loop, in the same shape, with the same residual/warning behavior
  — exactly how GPU/vast was added alongside LLM.
- **A new provider's prices:** add them to `pricing.py` with a source (the audit enforces it).

## 13. Maturity & honest gaps

What's solid: the enforcement core + reconcile + pricing are well-tested (81% on the money path); zero-dep,
cross-platform, cross-process; the no-hardcoding + estimate-first disciplines are enforced in CI. What's
deliberately *not* unit-tested to high coverage: the I/O adapters (claude.ai chat, transcript parsing, the
paid-call `compare`/`cachetest` dev tools) — integration-tested instead. Roadmap + open items: [ROADMAP.md](ROADMAP.md).

## 14. Appendices

- **CLI reference:** the [README](https://github.com/llmspendguard/llm-spendguard/blob/main/README.md#cli--full-command-reference) (`enforce`, `reconcile`, `report`,
  `schedule`, `resources`, `advise`, `optimize`, `brief`, `worklog`, `tag`, `experiment`, `compare`, `bootstrap`, …).
- **Env knobs:** `SPENDGUARD_HOME`, `GATE_DISABLE`, `GATE_ALLOW`, `GATE_CAP`, per-class `GATE_<CLASS>_<WINDOW>`,
  `GATE_META_BUDGET`, `SPENDGUARD_SAAS_KEY`, `SPENDGUARD_PRICES` — see [README §Knobs](https://github.com/llmspendguard/llm-spendguard/blob/main/README.md#knobs-env).
- **Learning advisor** (cold start, corpus, living insights, collective learning): [learning-advisor.md](learning-advisor.md).
- **Server contract + the other half of the journey:** [server Solution Spec](https://github.com/llmspendguard/llm-spendguard-server/blob/main/docs/SOLUTION-SPEC.md).

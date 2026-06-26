# Architecting Win — the enterprise quality playbook

> A reusable, end-to-end standard for taking a solution from "it works" to **enterprise-grade**: secure, reliable,
> correct, forward-compatible, easy to use, and *honestly* documented. It is not just principles — it is a **literal
> checklist of what must be true**, the **processes to run** to prove it, the **actions** to take, and a library of
> **LLM prompts** you (or an agent) can run to enforce it on this solution or any other.
>
> It was distilled from hardening **llm-spendguard** (the open-source client) and **llm-spendguard-server**
> (the multi-tenant SaaS) to enterprise level. Every item below is backed by a real change in those repos — cited
> as evidence in §9 so the checklist is grounded, not
> aspirational.

**How to use this document**

1. Read §1 Principles once — they are the *why* behind every checklist item.
2. For any solution, run the §3 checklists and the
   §4 processes. Treat unchecked boxes as work, not as decoration.
3. Use the §5 LLM prompts to have an agent *adversarially*
   verify each dimension — find the gap before a customer (or an attacker) does.
4. Ship only when §6 Definition of Done passes. Record the gaps you chose
   not to close (§1.8 honest maturity) — that section is mandatory, not optional.

---

## 1. Principles — the *why*

These are the load-bearing ideas. Each is testable; each maps to checklist items below.

### 1.1 Fail-closed for money, fail-open for the call path
The thing that protects the user (a spend cap, an auth check, a tenant boundary) must **fail closed** — deny when
uncertain. The thing that must never break the user's work (observability, a governance hook, a non-critical
enrichment) must **fail open** — degrade, log, continue. Decide which is which for *every* control, and write it down.

### 1.2 Privacy by not collecting
The most valuable secrets (keys, prompts, PII) should **never be sent or stored** — so a full compromise still
can't disclose them. Achieve privacy by *architecture* (don't collect), not by *promise* (collect then guard).
Re-validate at the trust boundary anyway (defense in depth).

### 1.3 Truth-anchored, residual-surfaced
Magnitude comes from an authoritative source (the provider bill, the database, the meter). Inference (an agentic
LLM read, a heuristic) only decides *attribution* — *who/what*, never *how much*. The unexplained remainder is
**surfaced as an explicit residual**, never silently dumped on a bucket or hidden. A failed fetch is `UNKNOWN`,
never a misleading `$0`.

### 1.4 Tolerant reader / forward + backward compatible
A live system evolves while old and new clients coexist. So: **ignore unknown fields, default missing ones, fall
back unknown enums** — never hard-error on shape drift. Version the wire (path + payload `schema_version` +
capability handshake). Migrations are **additive-only** by default; breaking changes get a new major + a
deprecation window. A years-old client must keep working.

### 1.5 No hardcoding — config- and taxonomy-driven
Nothing customer-, account-, or environment-specific is baked into shipped code: no hardcoded paths, URLs, org
names, prices, or identities. It comes from config, the user's own taxonomy, or a verified single-source table
(and a CI audit fails the build on a stray literal).

### 1.6 Estimate before you spend (the spend protocol)
Any paid/irreversible batch action runs a **separate, zero-cost estimate** (count + cost) that is confirmed
*before* the spend. Never cancel a running paid job as cost control — completed work still bills. Cap scope in the
code (abort if count exceeds a bound).

### 1.7 Decouple I/O from pure logic (fetch → transform → load)
Correctness lives in **pure functions** (no network, no DB, no print) that are trivially unit-testable; the I/O is
thin shells at the edges. This is the same **map/reduce** shape everywhere (map each source → records; reduce →
the rollup), so the testable core grows and the integration surface stays small.

### 1.8 Honest maturity
Document what is *not* done — the accepted residual risks, the scale ceilings, the deferred hardening — as a
first-class section. A credible engineering artifact states its gaps; marketing hides them. "We tested the
money-critical core to 81%; the I/O adapters are integration-tested, not unit-tested, by design" beats a vague
"well-tested."

---

## 2. Quality dimensions (what "enterprise-grade" means)

| Dimension | The bar | Proven by |
|---|---|---|
| **Secure** | Auth, tenant isolation, secret handling, audit trail, disclosure policy, threat model — each enforced, not promised | §3.1 · §4.1 |
| **Correct** | Tolerant reader, idempotent, reconciles ("adds up"), no double-count, money math right | §3.2 · §4.2 |
| **Reliable / operable** | SLOs + error budget, DR runbook, forward-only migrations, observability, known scale ceiling | §3.3 |
| **Forward-compatible** | Versioned envelope + capability handshake, additive migrations, deprecation policy | §3.4 |
| **Well-tested** | Deterministic offline tests, **scoped** coverage gate on the critical core, CI gates, no silent no-op tests | §3.5 · §4.3 |
| **Sound design** | Adapter seams, pure transforms, no hardcoding, single source of truth | §3.6 |
| **Easy to use** | Zero/low-dep install, plain-English over jargon, data-driven (never faked), honest labels | §3.7 |
| **Documented** | Solution spec, threat model, ops, migrations, complete env example, honest gaps | §3.8 |

---

## 3. The assurance checklists (what must be true)

> Copy these into a PR template / release issue. An unchecked box is a tracked task. "N/A" must have a one-line reason.

### 3.1 Security
- [ ] **AuthN**: every entry point authenticates; secrets compared **timing-safe**; no secret in argv/logs/URLs.
- [ ] **AuthZ / tenant isolation**: enforced by the **datastore** (e.g. FORCE RLS), not by convention; verified by a
      test that runs **as the least-privileged role** and proves a cross-tenant read returns nothing even with an
      explicit filter; a bypass/superuser test proves it's the mechanism, not the fixture.
- [ ] **Least privilege**: the app role can do only what it needs; privileged tables/ops are owner-only.
- [ ] **Secret handling**: secrets in env/managed store only, never in the repo; an `.env.example` documents *every*
      one; decryption is in-process (never on argv); a leak path is a vulnerability.
- [ ] **Secret scanning**: CI fails on a *verified/live* secret (not on placeholders); push protection on.
- [ ] **Audit trail**: every security-sensitive mutation (key mint/revoke/rotate, role grants, billing, deletion) is
      audit-logged with actor + timestamp + detail. Data mutations keep a version history.
- [ ] **Input validation at the boundary**: reject secrets/PII even if the client already scrubbed (defense in depth).
- [ ] **SSRF / egress**: outbound requests refuse non-HTTPS / attacker-named endpoints; never send user data to a
      URL that came from observed content.
- [ ] **Rate limiting**: per-caller, with `429 + Retry-After`; documented limits.
- [ ] **Webhooks**: signatures verified before any state change; idempotent.
- [ ] **Fail-closed**: a missing auth secret → deny (e.g. cron returns 401 without its secret), never open.
- [ ] **Threat model exists**: trust boundaries, assets, STRIDE-style threats → the in-code mitigation for each, +
      accepted residual risks.
- [ ] **Disclosure policy**: a `SECURITY.md` (private reporting channel, response targets, scope, supported versions).
- [ ] **Dependencies**: automated update PRs (minor/patch + security auto; majors deliberate) + an audit in CI.
- [ ] **SAST/DAST**: static analysis (CodeQL or equiv) on every push; an on-demand dynamic scan against a preview.

### 3.2 Correctness
- [ ] **Tolerant reader**: extra fields ignored, missing fields defaulted, unknown enums fall back — never a hard
      error on shape drift. Locked by a test.
- [ ] **Idempotency**: every write is keyed + upsert; re-running produces no double-count; a watermark prevents
      re-processing.
- [ ] **It adds up**: totals reconcile against an authoritative source; `truth − captured − attributed = residual`,
      and the residual is surfaced. Tested across date-trims (the "copy-then-trim" test).
- [ ] **No double-count under aggregation**: splitting a quantity across dimensions (project/team) sums back to the
      whole; turns/events counted once.
- [ ] **Money math**: prices from a single verified table (never hardcoded); cache/discount tiers correct; unknown
      model → safe `$0`, never a crash; a CI audit fails the build on a mispriced literal.
- [ ] **`UNKNOWN` ≠ `0`**: a failed external fetch yields `None/UNKNOWN` + a loud warning, never a misleading zero
      that reads as "100% covered."
- [ ] **Determinism**: no reliance on wall-clock/`now()`/random in logic that's asserted; pass time in.

### 3.3 Reliability & operations
- [ ] **SLOs** defined (availability, p95 latency, job success, accuracy bound) + an **error budget** policy.
- [ ] **DR**: documented RTO/RPO; a **step-by-step restore runbook**; backups verified; the schema reproducible
      from migrations.
- [ ] **Migrations**: forward-only, ordered, **checksum-tracked**, transactional; the **same runner builds test and
      prod** (no drift); additive-only enforced in CI.
- [ ] **Observability**: structured logs (ids + counts, never PII), error monitoring (loaded only when configured),
      a defined alert list.
- [ ] **Scale ceiling documented**: where the current design stops scaling (e.g. a sequential cron loop) is *written
      down*, with the next step — not a silent cliff.
- [ ] **Rollback**: a bad deploy reverts fast (stateless functions / instant rollback).
- [ ] **Config completeness**: the app starts from documented config alone; degrades gracefully when optional
      config is absent.

### 3.4 Forward & backward compatibility
- [ ] **Path-versioned API** (`/v1`); breaking change → `/v2` side-by-side for a deprecation window with
      `Deprecation`/`Sunset` headers.
- [ ] **Payload `schema_version`** + a **capability handshake** (a health/discovery endpoint advertises version +
      features) so a newer client degrades against an older server and vice-versa.
- [ ] **Versioned message envelope** for every async message (queue commands, events): `{ v, type/kind, id, ts,
      payload }`, kind validated against a known set, tolerant on read.
- [ ] **Per-row versioning + history** for evolving records (audit trail of how a row changed).
- [ ] **Additive-only migrations** (add column + backfill + deprecate; never drop/rename in place without an
      explicit, reviewed marker).

### 3.5 Testing & quality
- [ ] **Deterministic + offline**: tests run with no network and no spend (SDKs/providers stubbed); isolated state
      (temp home/DB), no touching the real environment.
- [ ] **Scoped coverage gate**: enforce a **high** floor on the **money-/safety-critical core** (not a vanity
      whole-repo number); I/O adapters are integration-tested by design — state that explicitly.
- [ ] **No silent no-op tests**: confirm each test *actually executes* (a fixture-style test under a script runner,
      or vice-versa, can pass while running nothing). Verify the count goes up.
- [ ] **CI gates**: lint, typecheck, build, the coverage floor(s), the price-literal audit, the additive-migration
      guard — all blocking.
- [ ] **Adversarial tests**: the security-critical invariant is proven *as the attacker* (cross-tenant, fuzzed
      ingest, forged webhook), not just happy-path.
- [ ] **Regression lock**: every bug fixed gets a test that fails before the fix.

### 3.6 Design & decoupling
- [ ] **Pure core**: business/correctness logic is in pure functions (fetch → transform → load); I/O shells are thin.
- [ ] **Adapter seams**: adding a provider/source/sink is one entry + one function, no edits elsewhere
      (interceptor registry, `Source` adapter, emit sinks).
- [ ] **Single source of truth**: one place for prices, schema, taxonomy — no divergent copies that drift.
- [ ] **No hardcoding**: paths, URLs, identities, prices are config/taxonomy-driven; the package ships generic.
- [ ] **Map/reduce shape**: parallelizable map per item + a reduce; barriers only when a stage truly needs all prior
      results.

### 3.7 Ease of use & UX honesty
- [ ] **Frictionless install**: zero/low required deps; one command; fails open if an optional dep is absent.
- [ ] **Data-driven, never faked**: every number on a screen traces to real data (grep the source — a literal like
      `$9080.53` in code is a red flag); no placeholder/sample data in prod paths.
- [ ] **Plain-English lead, detail on demand**: lead with the answer + a one-line interpretation; tuck statistics/
      jargon into a disclosure. De-jargon internal tags (`llm-spendguard` → "our own cost").
- [ ] **Honest labels**: "estimated", "value (not billed)", "≥ X certain" — never overstate. Lead with the bigger
      lever (e.g. a $632 loss over a $58 saving).
- [ ] **No redundant surfaces**: two panels showing the same thing → merge.
- [ ] **Explain "why static/empty"**: if a panel reflects pushed data, say when it last updated + how to refresh.

### 3.8 Documentation
- [ ] **Solution spec** (umbrella): why it exists, the value, the **end-to-end journey** (of a request/dollar/row),
      key concepts, architecture, security, ops, testing, extensibility, **honest maturity** — links into focused docs.
- [ ] **Threat model**, **operations** (SLO/DR/observability), **migrations policy**, **API + versioning + rate
      limits**, **data model**.
- [ ] **`.env.example` complete**: every `process.env.*` / config key documented (verify: code-vars − example-vars = ∅).
- [ ] **READMEs** point to the spec first; cross-repo links resolve; docs build clean (e.g. `mkdocs --strict`).
- [ ] **CHANGELOG** per meaningful change.

---

## 4. The processes to run (how to prove it)

### 4.1 Adversarial red-team
Run a multi-dimension review and **adversarially verify** each finding before fixing (default findings to "real
only if you can reproduce"). Dimensions: security, correctness, reliability, smell/taste, docs drift. For each
finding: reproduce → fix → add a regression test → re-verify. *Spendguard waves found: phantom cross-org GPU spend,
an LLM double-count, a secret on argv, a fetch-failure swallowed as $0, an over-deleting prune — each fixed + tested.*

### 4.2 "Does it add up" (reconciliation)
Build a known fixture, **trim it by date** at several cutoffs, and assert: per-source `truth − captured −
attributed = residual` (constant under trim), pivots sum to the headline, and the portfolio reconciles across all
sources. A failed external read must surface as `UNKNOWN`, never $0. (Spendguard: `test_reconcile_e2e.py`.)

### 4.3 Coverage gate scoped to what matters
Measure coverage; gate the **critical core** at a high bar (spendguard: 78% money-core, today 81%) and the whole
package at a regression floor (40%) — separately. Adding a test that imports a new module can *lower* the aggregate
by pulling untested functions into the denominator; that's the gate being *honest* — cover them, don't lower the bar.

### 4.4 Migration discipline
`migrations/NNNN_*.sql`, immutable + checksum-pinned, applied in one transaction with a tracking row; the same
runner builds the test DB. A destructive op requires an explicit `-- @destructive: <reason>` marker; CI fails
otherwise.

### 4.5 Automated security scanning (CI)
SAST (CodeQL on public; document the GHAS toggle for private), secret scanning (verified-only, so placeholders
don't false-positive — and note the gitleaks *action* needs an org license; the binary/TruffleHog don't),
dependency audit (report-only + Dependabot fix PRs), Lighthouse (warn-only) + an on-demand OWASP ZAP baseline
against a preview.

### 4.6 The spend protocol (for any paid run)
Estimate (separate, zero-cost) → review the rendered prompt on known-answer examples → confirm → run, under a gate
that **fails closed** if not actually enforcing. Cap scope in code. Never cancel-to-save.

### 4.7 Release
Lint + typecheck + build + tests + coverage floors + price audit + additive-migration guard all green; CHANGELOG
updated; docs build clean; CI green on the exact commit before declaring done.

---

## 5. The LLM prompt library (run the checks with an agent)

Paste a prompt, point it at the diff/repo, and require **evidence + a verdict** (file:line, reproduce, real/not).
Each is written to make the model *try to break it*, not bless it.

**5.1 Security red-team**
> You are an adversarial security reviewer. For the code in <scope>, enumerate threats by trust boundary (entry →
> auth → data → egress). For EACH: state the attack concretely, cite file:line, and say whether a working exploit
> exists (default: not-real unless you can reproduce). Specifically check: tenant isolation enforced by the DB not
> convention; secrets never in argv/logs/URLs; timing-safe secret compares; fail-closed auth; SSRF/egress guards;
> every security-sensitive mutation audit-logged; input re-validated at the boundary. Output a table:
> threat · boundary · file:line · exploit? · mitigation present? · fix.

**5.2 Correctness / "does it add up"**
> Verify the money/aggregation logic in <scope> reconciles. Construct a known fixture; trim it by date at 3 cutoffs;
> assert truth − captured − attributed = residual at each, that splits across dimensions sum back to the whole (no
> double-count), and that a failed external fetch yields UNKNOWN not $0. Show the arithmetic. List any place a
> number could be wrong with a reproducing input.

**5.3 Fake-data / hardcoding / silent-no-op hunt**
> Find anything that is NOT genuinely data-driven or that silently does nothing. (a) grep for literal magic numbers/
> names that look like real values baked into code; (b) find tests that can pass without executing (fixture-style
> under a bare-script runner, or a `|| true` that hides failure); (c) find hardcoded paths/URLs/identities/prices.
> For each: file:line + why it's a risk + the config/taxonomy-driven fix.

**5.4 Forward-compat / tolerant reader**
> For each ingest/queue/event consumer in <scope>, prove it is a tolerant reader: feed it (a) a payload with unknown
> extra fields, (b) one missing optional fields, (c) an unknown enum value — assert it ignores/defaults/falls-back
> and never hard-errors, while still rejecting genuinely invalid/secret content. Check for a versioned envelope +
> capability handshake. List violations.

**5.5 Honest-maturity critic**
> What is NOT done here? List the accepted residual risks, scale ceilings, untested surfaces, and deferred hardening
> that an enterprise auditor would flag. For each: the risk, why it's acceptable (or not) today, and the trigger
> that should force the work. Be the skeptic the README isn't.

**5.6 Smell / taste**
> Review <scope> for smells: duplicated surfaces that should merge, jargon a user won't understand, panels/numbers
> with no clear action, leaky abstractions, dead code, comments that contradict the code, inconsistent naming. For
> each: the smell, why it hurts the reader/user, and the cleaner shape.

**5.7 Docs completeness**
> Verify the docs match the code: every `process.env.*`/config key is in the env example (list any missing); every
> public command/endpoint is documented; the solution spec covers why/value/journey/design/security/ops/testing/
> maturity; cross-links resolve; the docs site builds strict. Output the gaps.

**5.8 Coverage-honesty**
> Report coverage for the money-/safety-critical modules separately from the whole repo. Identify functions in the
> critical set with 0 coverage and the highest-value untested branches. Do NOT propose lowering a threshold; propose
> the tests that raise it.

---

## 6. Definition of Done — the ship gate

A solution ships when **all** are true (or the exception is recorded in §1.8 maturity):

1. Every box in §3 checked or explicitly N/A-with-reason.
2. The §4 processes run; findings fixed + regression-tested.
3. The §5 prompts run; no unaddressed real finding.
4. CI green on the exact commit: lint · typecheck · build · tests · coverage floors · price audit ·
   additive-migration guard · security scan.
5. Docs build clean and lead with the solution spec; `.env.example` complete; CHANGELOG updated.
6. An **honest maturity** section lists what was deliberately deferred + the trigger to revisit.

---

## 7. Anti-patterns (the tells that you're not there yet)

- A vanity coverage number over the whole repo instead of a high bar on the critical core.
- Tenant isolation "by WHERE clause" / by convention instead of enforced by the datastore.
- A failed fetch rendered as `$0` / "100% covered."
- Hardcoded prices/paths/customer names in shipped code.
- Two dashboard panels that are the same data; jargon with no action.
- A migration that drops/renames a column in place with no deprecation.
- `sys.exit()` deep in a library (escapes `except` guards — a real spendguard bug); cancelling a paid job "to save."
- A README that claims "secure/reliable/enterprise" with no threat model, SLOs, or honest-gaps section.
- Tests that pass without executing (fixture-style under a script runner).

---

## 8. Quick-start: applying this to a NEW solution

1. Write the **solution spec skeleton** (§3.8) first — the journey of a request forces the architecture out.
2. Stand up **CI gates** early (§4.7) — lint/typecheck/build/test + the coverage floor — so quality can't regress.
3. Make the **critical core pure** (§1.7) and test it offline + deterministically (§3.5).
4. Enforce **isolation/auth in the datastore** (§3.1) and prove it adversarially (§5.1).
5. Version the **wire + schema** (§3.4) from day one — retrofitting forward-compat is painful.
6. Run §4 + §5, fix, and only then claim a dimension "done." Keep §1.8 honest.

---

## 9. Case study — how spendguard was hardened (evidence)

Each principle/checklist item above is backed by a real change. A representative trace (client `llm-spendguard`,
server `llm-spendguard-server`):

| Principle / item | What was done | Where |
|---|---|---|
| Tenant isolation, proven adversarially (§3.1) | FORCE RLS via a non-owner role; tests run **as** `spendguard_app` proving cross-tenant reads return nothing + a superuser-bypass control | server `tenancy_billing.test.mjs`, `0001_baseline.sql` |
| Audit trail (§3.1) | Key **mint/revoke/rotate** were unaudited → added actor-threaded `audit_log` + DB test | server `1815b0b` |
| Threat model + disclosure (§3.1) | Trust-boundary STRIDE doc + `SECURITY.md` (both repos) | `docs/THREAT-MODEL.md`, `SECURITY.md` |
| Automated security scan (§4.5) | CodeQL (public client) + TruffleHog verified-only + Dependabot + ZAP + Lighthouse | `29df88c`, `.github/workflows/security.yml` |
| It adds up (§3.2, §4.2) | Account-anchored reconcile; `truth − captured − attributed = residual`; the copy-then-trim E2E | client `reconcile.py`, `test_reconcile_e2e.py` |
| UNKNOWN ≠ 0 (§3.2) | A failed provider fetch → `None` + loud warning, never $0 | client `reconcile.residual` |
| Forward-compat (§3.4) | Tolerant-reader test + capability handshake in `/v1/health` + additive-migration CI guard + versioned message envelope (commands + events) | server `forward_compat.test.mjs`, `migrations_additive.test.mjs`, `command_envelope.test.mjs`; client `emit.envelope` |
| Migrations (§4.4) | Dual schema.sql/ad-hoc-scripts → ordered checksum-tracked runner, one path for test+prod | server `scripts/migrate.mjs`, `docs/MIGRATIONS.md` |
| Coverage gate, scoped (§4.3) | 78% floor on the money-core (today 81%) + 40% package; server `src/lib` 75/65/80 | both `ci.yml`, `5ab0776` |
| No silent no-op tests (§3.5) | A fixture-style `test_schedule.py` would have run nothing under the script runner — rewritten script-style | client `1992cb7` |
| Decouple (§1.7, §3.6) | `fetch→transform→load` extracted across report/saas; chat/conv/claudecode pure transforms locked with tests | client `5ac16f3`, `0d22fec`, `a3c4235` |
| No hardcoding (§1.5) | Agentic GPU discovery made taxonomy-driven; account-specific scripts removed; legal/seed env-driven | client `e049c51`; server `498e576` |
| Library must raise, not exit (§7) | `load_key` used `sys.exit()` → escaped `except` guards → `doctor` crashed without a key; changed to raise | client `4efef5f` |
| UX honesty (§3.7) | Real-vs-faked verified (no literals in source); merged redundant panels; loss-led Spend-efficiency reframe; learnings-freshness line | server `1771af5`, `ed311d0` |
| Honest maturity (§1.8) | Every spec ends with a tracked "gaps / what's next" section | both `docs/SOLUTION-SPEC.md`, `OPERATIONS.md §6` |

---

*This document is itself subject to §1.8: it is a living standard. When a new failure mode is found, add the
checklist item, the process, and the prompt that would have caught it — so the net tightens over time.*

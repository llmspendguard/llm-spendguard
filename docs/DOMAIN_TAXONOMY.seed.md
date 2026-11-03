# llm-spendguard — Domain Taxonomy (SEED hypothesis, v01)

> **Status: SEED, pre-cross-check.** This is the hand-locked hypothesis the team agreed on 2026-11-02,
> to be validated by an independent agentic Phase-A discovery pass on two $0 lanes. Where discovery and
> this seed disagree, the disagreement *is* the decision to adjudicate. This file exists so the taxonomy
> is **durable in-repo** — the v1 proposal (`PHASE0_SPEC_PROGRAM_PROPOSAL.md`, `placement_map.json`, the
> drafted GLOSSARY/PRINCIPLES) lived only in a scratchpad and was lost when it recycled. That must not
> happen again: the spec lives here.

## 0. The reframe that drives this — spendguard is a multi-currency **token bank**

A dollar is a dollar; a token is not a token. We are building an **accounting, forensic, and
economic-valuation engine** over a bank whose currencies are not fungible.

| Banking construct | Token-bank meaning |
|---|---|
| **Currency / denomination** | provider × model — non-fungible, non-transferable (N wallets you cannot rebalance) |
| **Allowance account** | subscription (Max / Pro / codex) — periodic, use-it-or-lose-it, resets, **zero salvage**; the $0 lanes. NOT a stored deposit |
| **Stored-value account** | prepay / credits — a real balance, drawn down, auto-reload at threshold |
| **Arrears account** | metered / pay-as-you-go API — real $, pay-per-use liability; the weekly-cap **overage** is arrears on the allowance |
| **Exchange rate** | quality-adjusted `$/good-for-intent = nominal_price ÷ quality_for_intent`. Nominal FX = `pricing.py`; the load-bearing rate is quality-adjusted. The only *exchange* happens at the **moment of spend** (which wallet pays this job) |
| **Valuation** | each denomination = an asset with a cost + quality-return distribution per intent; routing = **assignment-under-budget** (bandit + knapsack), NOT Markowitz |
| **Consolidation** | user → team → org = chart-of-accounts consolidation |

Three account **classes** that **structurally cannot be summed** (CLAUDE.md rule #7):
cash/arrears (**real-$**), allowance-consumed (**est-value**), cost-avoidance (**savings**).

## 1. Why v1 was incomplete — a structural fault, not a counting error

v1 was **5 layers / 21 domains**. A single-pass classifier placed all 181 modules with `needs_home=0` and
reported "100% coverage / healthy distribution." That was the trap: it achieved zero-homeless by **smearing
homeless modules across the Mechanism layer** instead of admitting a domain was missing. The tells:

- **7 of 14 economics probes did not land in Treasury** — `lane_bandit`→learning, `lane_quota_cost`→lanes,
  `route_realized`→measurement, `route_horizon`→queueing, `whole_job`→queueing, `overage`→reconcile,
  `weekly_cap_guard`→queueing.
- **106 modules carried cross-domain split flags** (>1 responsibility).
- **Warm-daemons / active-spend had no home** (not cli, not orgsync).

Root cause: v1 organized by **layer** (L0→L4 dependency order) but never by **what a module does**. The five
capability kinds — **GENERATE · ACTIVELY-MANAGE · STORE · ANALYZE · INTERFACE** — were the missing axis, so
**active-management** and **economic-analysis** had no column to live in.

**The law that replaces "healthy distribution":** a module with no clean home means a **missing domain**,
never a smeared placement.

## 2. The spine — capability kind is primary, layer is a secondary attribute

**10 domains.** (Natural number once active-management and analysis get their own columns. Flexes only on
calls we have now closed — see §4.)

### STORE — systems of record
1. **Config & Pricing** `(L0)` — `config`, `pricing` (the FX table), model-catalog SSOT, name-registry. The
   denominations and exchange rates. *Reference data; depends on nothing internal.*
2. **Ledger** `(L0)` — `money-ledger` (double-entry GL, chart of accounts), connection pool, retention,
   backup. **Money truth.**
3. **Spend Search** `(L0/L1)` — `ledger_read`, query, the read-only snapshot path. *The "ledger AND its
   search as its own section."* Reads only; owns its own per-operation query budgets.

### GENERATE — produce the spend and the data
4. **Execution** `(L1)` — `executors`, `adapters`/`vendor_call`, batch submit/collect, `remote`/vast,
   http-capture. Spends tokens, emits ledger events.

### ACTIVELY-MANAGE — live money management & active use of tokens
5. **Routing & Admission** `(L1)` — routing, lane selection, `gate.require`, admission control (tpm),
   `weekly_cap_guard`★, shedding, fallback ladder. **The per-call decision: which wallet pays this job.**
6. **Token Operations** `(L1)` — queue/dispatch, delegation (codex substrate, pool + orphan-reaper),
   warm-daemons★ (codex pool, warm lane pool, warm MCP daemons), utilization, `whole_job`★ planner. **The
   cross-call resource management** — the missing "money management and active use of tokens."

### ANALYZE — the economic-valuation engine, the data science, and the forensics
7. **Treasury / Valuation** `(L1↔L2 read-side)` — the economic-valuation engine: quality-adjusted `$/good`,
   best-value resolver, the **materialized valuation table**, `lane_quota_cost`★, balances, budget, the
   Allowance / Stored-value / Arrears account model. *Values and decides economics at spend-time.*
8. **Measurement & Learning (Economic Analysis)** `(L2)` — measurement, the `lane_bandit`★ **learning
   write-path**, `route_realized`★, `route_horizon`★, bakeoff, calibrate, determinism, savings analytics.
   *The data-science home — observes history and produces the estimates Treasury consumes.*
9. **Reconciliation & Attribution** `(L3)` — bank reconciliation (saas reconcile, truth push), attribution
   (job / conversation / chain), `overage`★ reconciliation, residency. *Forensic accounting + the 3rd axis.*

### INTERFACE — thin facades that own no capability
10. **Surfaces** `(L4)` — **one domain, three sub-contexts: `cli` · `mcp` · `api`** (+ diagnostics,
    orgsync), **generated from the usage-surface Capability SSOT** (CLAUDE.md usage-surface doctrine). Thin
    facades that call into 1–9; they own no capability. Parity + discoverable docs hold **by construction**,
    enforced by the SSOT conformance test.

★ = a module that was homeless or misplaced in v1 and now has an obvious home.

## 3. The acyclic economic loop (why A is split, and split *smart*)

```
Ledger(2) ──► Measurement&Learning(8) ──► [valuation table] ──► Treasury(7) ──► Routing(5) ──► Execution(4) ──► Ledger(2)
                    (writes estimates)        (hand-off)         (reads/values)    (decides)      (spends)
```

**A — Treasury (7) vs Measurement & Learning (8): split, smart.** The **materialized valuation table is the
one hand-off artifact**. Measurement & Learning owns the *write-path* (observe outcome → update the posterior
→ write estimates); Treasury owns the *table + read-path* (materialize estimates × pricing × balances into
`$/good`, then decide). They communicate **only through the table — no back-edge** — so the loop stays
acyclic. `lane_bandit` is the deliberate **seam**: its learning-update half is domain 8, the table it
materializes and every spend-time read of it are domain 7.

**B — Routing & Admission (5) vs Token Operations (6): two (operator's call, taken).** #5 is the *per-call*
decision (which wallet pays); #6 is *cross-call* resource management (pools, queue, delegation, warm-daemon
lifecycle). Different cadence, different failure modes → two domains.

**C — Surfaces (10): one domain, three sub-contexts (cli/mcp/api), SSOT-generated.** They share one facade
contract; splitting into three top-level domains adds no real boundary. The capabilities are declared once in
the usage-surface SSOT and each surface is generated from it.

## 4. Cross-check against the last ~2 days of work (2026-10-31 → 11-02)

The taxonomy is not abstract — recent work touches **6 of 10 domains and confirms the boundaries**; no new
domain was revealed, and two boundaries sharpened into principles.

| Recent work | Domain(s) | What it confirms |
|---|---|---|
| **Ambient-scope / `LedgerQueryDeadline` wedge** (#288/#289/#291, 0.16.28): `effective_ledger_admission_deadline` (ledger_read), `require_ledger_writer_request_live` (ledger_writer), MCP `do_POST` ambient scope + `classify` | 2, 3, 10, (8/5 for classify) | **The exact bug was a Surface-layer cancellation deadline leaking into STORE-domain operation budgets.** The new model makes that a boundary violation by construction → **Principle P1**. |
| **Catalog SSOT + migration 0014** (server #43; `as_of`=source, `ingested_at`=load) | 1 | Catalog = denomination/pricing reference; provenance = STORE metadata. Confirms #1 owns pricing+catalog SSOT and its provenance. |
| **Warm daemons**: codex pool size + orphan-reaper, warm lane pool, warm MCP daemons, warm-hook :4912 | 6, (10 for serving) | Confirms #6 is real and rescues the homeless daemons. Sharpens **serving vs lifecycle** → **Principle P2**. |
| **Capability SSOT doctrine + surface audit** (278 caps, 240 gaps) | 10 | The audit is the evidence Surfaces MUST be SSOT-generated. Confirms **C** and gives #10 its conformance test. |
| **honestreview codex integration, precommit $0-lane, warm delegate** | 5, 6 | The delegate substrate lives in #6; lane selection in #5. |
| **ccwatch lane-leak fix** (#11; →$0-lane best-value, `SpendGateRefused`=DEFER) | 5, 7 | Confirms #7 owns best-value valuation. |

### Principles this yields (domain laws)
- **P1 — A facade never times out a domain's own work.** A Surface (10) imposes only its own request
  lifecycle; STORE (2,3) and ANALYZE (7,8,9) operations each carry their own **per-operation budgets**. The
  ambient-scope wedge was precisely the violation of P1; the taxonomy makes reintroducing it a visible
  cross-domain contract break. (This is exactly #291 Option 1, promoted to a law.)
- **P2 — A served capability and its lifecycle are different domains.** A warm MCP daemon's *serving* is
  Surfaces (10); its *pool / lifecycle / reaper* is Token Operations (6). This is how the "106
  multi-responsibility" cases resolve — by separating the two responsibilities across their true domains.
- **P3 — Three account classes, never summed** (real-$ / est-value / savings). Owned by 2+9 (cash/arrears),
  7 (allowance-consumed), 9 (cost-avoidance).
- **P4 — The economic loop is acyclic** (§3). Enforced by the valuation-table hand-off.
- **P5 — No clean home ⇒ a missing domain**, never a smeared placement or "healthy distribution."

## 5. Phase-A discovery — the independent cross-check

Discovery is itself an LLM judgement, not a decree. Acceptance test, run on **two independent $0 lanes**:

- **Evidence corpus:** all 181 modules + a one-line "what it does" per module + the import/AST edges
  (symgrep/codebygraph) + the three failure signals (106 splits, 7 economics misses, homeless warm-daemons)
  + the five capability kinds + this seed.
- **Ask each lane to propose a domain structure** independently; then cross-check the two proposals against
  each other **and** against this seed.
- **Hard acceptance:** every module lands in **exactly one** domain with a clean fit; any module without one
  **names a missing domain** (P5), never "healthy distribution."
- **Output:** the divergences from this seed — those are what the operator adjudicates. Agreement promotes
  the seed to the committed taxonomy; divergence is a decision, not an auto-apply.

Surfaces (10) are explicitly **thin** in the brief — a facade that calls into other domains, never a
capability owner.

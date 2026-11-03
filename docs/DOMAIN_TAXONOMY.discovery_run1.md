# Domain discovery — run 1 cross-check (seed hypothesis vs independent re-derivation)

**Date:** 2026-11-02. **Corpus:** 220 `src/spendguard/*.py` modules (built by
[`scripts/probe/build_domain_corpus.py`](../scripts/probe/build_domain_corpus.py), full docstrings un-truncated).
**Discovery output:** [`DOMAIN_TAXONOMY.discovery_run1.json`](DOMAIN_TAXONOMY.discovery_run1.json) — one holistic DDD
pass over the whole corpus (not per-file: a bounded-context analysis needs every module + its import edges in one
context). **Seed under test:** [`DOMAIN_TAXONOMY.seed.md`](DOMAIN_TAXONOMY.seed.md) (10 domains, 5 kinds).
**Finalized taxonomy:** [`DOMAIN_TAXONOMY.md`](DOMAIN_TAXONOMY.md) (7 kinds × 21 contexts).

## Coverage
All 220 modules placed exactly once (domain lists + `split_modules` = 220, none missing, none duplicated) — verified
by count. `homeless: []`. So the diff below is complete, not a sample.

## Verdict: seed partially confirmed

### Confirmed
- The **capability-kind spine** is the right primary axis.
- Ledger, Spend Search, Execution, Routing, Token Operations, Treasury, Reconciliation are real bounded contexts.
- **Seam A (Treasury vs Measurement & Learning) — the flagged highest-risk boundary — reproduced**, and *finer*: the
  discovery splits Treasury from Measurement **and** Measurement from Learning (3 contexts vs the seed's 2). "Split
  smart" was correct; the loop names measurement and learning as separate stages, so one domain would hide the
  measurement→learning edge.
- All 7 economics modules got a clean home. Three land differently than the seed said:
  - `overage` → **Treasury** (live Arrears status), not Reconciliation (not forensic accounting).
  - `best_value` → **Routing** (selects a model), not Treasury (does not value one).
  - `whole_job` → **split** Routing / Token Operations.

### Divergences (seed too coarse or wrong)
| Seed | Discovery |
|---|---|
| **B:** Routing & Admission = one domain | **Two** — *which wallet pays* (Routing) vs *allow/deny/defer* (Admission & Enforcement). |
| **C:** Surfaces holds reporting + orgsync | Surfaces stays **one** INTERFACE domain ✓, but **Spend Reporting** (computes totals) and **Federation & Egress** (owns scrubbing) pull **out** — neither is a thin facade. |
| 10 domains | **21** bounded contexts. |
| 5 capability kinds | **7** — +EGRESS, +PLATFORM. |
| Config & Pricing together | **Reference Data** (prices/catalog) split from **Configuration**. |

### `missing_domains_found` (9) — the P5 signal (no clean home ⇒ a missing domain)
Spend Capture & Ingestion · Call Corpus & Content Retention · Cost Estimation · Work Integrity ·
Learning & Advisory (split out of seed "Measurement & Learning") · Spend Reporting · Assurance & Self-Verification ·
Federation & Egress · Platform: Install, Fleet & Release.

### Two new capability kinds (the spine was incomplete)
- **EGRESS** — `deid/saas/signal/share/emit/notify/focus_export` own scrubbing/projection on the way off-machine, so
  they are not thin INTERFACE.
- **PLATFORM** — version/release/attestation/fleet/scheduler/install/run-gating: the product operating itself; none
  of the five kinds covered it.

## Splits
12 real multi-responsibility modules (vs **106** flagged in the earlier v1 attempt — the rest were false positives).
Largest: `adapters.py` (~90 defs: execution + live lane-health state + routing defaults + capability facts); its
lane-cooling state belongs in `resource_state`. Full table in [`DOMAIN_TAXONOMY.md` §2](DOMAIN_TAXONOMY.md).

## Findings beyond taxonomy (fed into the finalized doc §3)
- **Acyclicity is violated today.** The discovery estimated a ~30-module cycle; a follow-up deterministic AST
  measurement ground-truthed it as **one 142-module SCC / 941 edges** (the discovery's named edges are ~2% of it).
  Neither the `__init__` split nor removing all layer-inversions reduces it meaningfully — it is a dense within-L1
  mesh through the split-hubs. See [`DOMAIN_TAXONOMY.md` §3](DOMAIN_TAXONOMY.md) for the measured reality and the
  contain-don't-cure decision. *(Lesson: the holistic LLM discovery under-counts dense import structure ~50×;
  acyclicity claims must be ground-truthed mechanically before any edge-cut is sequenced.)*
- **`__init__` hub:** 185 modules import the package root, whose `__init__` installs the gate as an import
  side-effect — flagged the single highest-leverage fix.
- `bulkgate` → `advisor`: Admission consumes the learning advisor instead of a calibrated estimate (a real violation).
- Meta-loop: 9 ANALYZE modules import `submit` (Execution) to buy judge calls — expose a caged meta-call port.
- Rule-4 at scale: 76 non-surface modules import `capability_cli_parsing` and define `main()` — facades not thin.

## Phantom seed modules
`executors`, `bulk_delegate`, `utilization`, `determinism`, "money-ledger"/connection-pool, `savings` do not exist in
the corpus (renamed or planned). Corrected in [`DOMAIN_TAXONOMY.md` §4](DOMAIN_TAXONOMY.md).

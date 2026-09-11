# QUALITY AT BEST COST — the active advisor + the value proof

**Status:** IN BUILD (2026-09-10). Grounded against 6 read-only sweeps (call path, bake-off, panels;
estate conversations, estate repos, value-proof surfaces). Owner confirmed the public sentinel
**`reasoning="best-value"`** and the whole-plan (Phase 0–4) scope.

## Mission — two pillars, one record

1. **Pillar A — spendguard ACTIVELY ensures quality at best cost.** A caller states its *intent* and,
   optionally, *how good the answer must be*; spendguard chooses the **model AND the reasoning effort**
   that meet that quality at the lowest cost, from what it has MEASURED — not from a hand-picked literal.
   Bake-offs sweep the effort axis and learn; panels apply the learnings without collapsing to one vendor.
2. **Pillar B — spendguard PROVES its value.** Every choice is booked as a per-decision counterfactual
   (what the naive/requested call would have cost at the quality actually held), kept on its own axis and
   surfaced in the receipt, the report, and the MCP — never summed into real $.

**The unifying insight:** Pillars A and B are the *same object* seen twice. A single **decision record** —
`(intent, requested_model, requested_effort → chosen_model, chosen_effort, actual_$, counterfactual_$,
saved_$, quality, quality_conf)` — is both the training signal the learner reads back AND the receipt line
that proves the saving. Build the record once; both pillars fall out of it.

> Not to conflate (owner's words): Part I is the estate context that *justifies* the design; Part II is the
> feature itself. They are kept separate on purpose.

---

## Part I — The estate, end-to-end

What breaks (or broke) when the estate USES spendguard, and where each is resolved. `FIXED` = already
shipped (the foundation this work stands on); `OPEN` = closed by this plan; `EXT` = an additional extension
(Part III). Pillar A = quality-at-cost, B = value-proof.

| # | Challenge (who hit it) | Pillar | Status | Resolved by |
|---|---|---|---|---|
| 1 | Callers hand-pick model AND effort per intent; the learn-loop is built but **unwired** (`experiment` never passes `efforts=`, `_measure` drops `v["effort"]`, no effort verdict recorded) | A | **OPEN** | **Phase 0–2** |
| 2 | Reasoning models return EMPTY (hidden reasoning eats the budget; empty reads as "no findings") | A | FIXED 09-10 | reasoning-budget self-heal (floor + empty-jump + LEARN + `_probe`) |
| 3 | Hand-sizing `max_tokens` destroys work — too low silently truncates, removed hangs; autotune only ever *shrank* | A | FIXED / **EXT** | self-heal (done) + **auto max_tokens from measured p90/p99 need, reasoning-aware, respects caller ceiling** (Part III) |
| 4 | Panel collapse — the lane bandit runs ONE model across a "5-vendor" panel; every prior cross-vendor review invalidated | A | FIXED 08-31 | `served_by`/`panel_providers`/`panel_integrity`; **Phase 3 must carry this guarantee forward** |
| 5 | Pinning a dead/slow lane hangs the caller (no per-item deadline) | A | FIXED | deadline advisor/guard, per-lane floors |
| 6 | The $0 gemini/agy lane misclassified quota oscillation as a permanent ceiling → gemini permanently bypassed | A | FIXED | agy `/usage` oracle, transient-vs-size, served-set namespace-aware |
| 7 | **533/535 calls carried no intent** — the forensic core failure; also starves the per-intent learner | B | FIXED | intent threaded end-to-end (prerequisite for ALL of A) |
| 8 | "Prove spendguard saved $X" exists only as a narrow, anonymous counterfactual | B | **OPEN** | **Phase 4** |
| 9 | `bulk_delegate` refuses whole fans and logs them as noise; path was inert | A | FIXED 09-10 | structured `reason` codes, config-gap-vs-cooling, doctor surface, `tiers`/`lanes set-model` |
| 10 | Estimate-before-spend friction; gate ran advisory not enforcing | A/B | FIXED (partial) | lifecycle eval gate (estimate→test→EVAL→run) |
| 11 | Cost-display confusion — real $ summed with est-value | B | ONGOING rule | 3-axis invariant; the new savings axis obeys it too |
| 12 | Ledger leaks — provider-billed spend the gate never saw | B | ONGOING | reconcile/reconstruct/close |
| 13 | Bake-offs run ad-hoc per project; the winner rots as a `model=` literal; `advise`/`recommend` never wired into a call path | A | **OPEN** | **Phase 1–2** (auto-apply) + **EXT** (auto-slate) |

**Cross-cutting patterns** (confirmed in warden, 7thsense, honestreview, SAGA, cothinking, mmg, lmm):
1. **Everyone hardcodes the panel slate and re-implements collapse guards** → auto-slate per intent with
   first-class *vendor-critical / never-substitute* semantics (warden's `VENDOR_CRITICAL`).
2. **Everyone hand-sizes `max_tokens`, having independently rediscovered the same lesson** → one auto
   sizer deletes the class.
3. **Manual bake-off → paste winner as a literal; `bakeoff()`/`recommend()` never called** → the single
   biggest opening; the intent corpus is already logged cleanly, only *selection* is still manual.
4. **`reasoning`/effort is pinned to a fixed low constant, never chosen by the job's precision need** →
   green field.
5. **The bandit optimizes cost but breaks attribution + cross-vendor independence** → the selector must
   split *fungible* (substitute OK, water-fill the idle $0 lane) from *vendor-critical* (pin, record who
   actually answered, fail closed if diversity is unverifiable).

---

## Part II — The build

Five phases. Phase 0 is the shared enabler; 1–4 can then land independently. Each phase ships with a guard
test (anti-amnesia doctrine). Anchors are point-in-time (2026-09-10).

### Phase 0 — Put EFFORT on the measured frontier (the enabler both pillars need)

Today `$/good` is aggregated per `(vendor:model)` only — `calls` and `call_io` have **no effort column**,
so per-effort rows silently merge and "the right effort" has zero storage. Nothing downstream can learn or
prove effort without this.

- Add a `reasoning` (effort ordinal actually sent) column to **`calls`** (migration loop exists at
  `calls.py:199`) and thread it through `calls.insert` (`calls.py:295`) / `calls.record_call`.
- Add the same to **`call_io`** (`callio.py:46`) via `record_io_sample` (`callio.py:91`).
- Key the frontier on it: `advise.evidence` aggregation key → `(intent, model, effort)` (`advise.py:57`);
  `calls.cost_summary` GROUP BY (`calls.py:345`); `advise.ranked` returns per-`(model,effort)` rows
  (`advise.py:73`) so `$/good` is sliceable by effort.
- Persist requested→chosen on the result at the provenance seam (`adapters.py:1616`): add
  `requested_effort`/`chosen_effort` next to the existing `substituted_from`/`model`.
- **Guard:** `tests/test_effort_on_frontier.py` — a recorded call keeps its effort; `advise.ranked`
  separates two efforts of one model; a legacy row (no effort) still ranks.
- **Spend:** $0 (schema + aggregation only).

### Phase 1 — The auto-selector: `reasoning="<sentinel>"` (Pillar A, call path)

A caller passes the sentinel instead of an ordinal; spendguard resolves `(model, effort)` for the ambient
intent to **meet the intent's measured quality bar at lowest `$/good`**.

- **Seam:** top of `adapters.call`, adjacent to the existing agentic model-resolution block
  (`adapters.py:430`) — the exact precedent (rewrites `model` transparently, caches, records
  `resolved_from`/`resolution`, recursion-guarded via `_resolve_guard`). Read intent as
  `(calls.current() or {}).get("intent")` (`adapters.py:1596`).
- **Decide (agentic where it's a judgement, arithmetic where it's a defined scale):** consult
  `advise.ranked(intent)` (`advise.py:73`) — cheapest `per_good` among models whose `good_rate ≥ bar`;
  effort = the cheapest effort that held quality (Phase 0). This is pure arithmetic on measured evidence
  → the **$0 realtime path**, no LLM. Escalate to `advisor.recommend_models(run=True)` (`advisor.py:281`,
  ONE meta-caged call, estimate-first) only when there is no clear frontier pick and the caller allows it.
- **Fallbacks (honest, never silent):** no evidence yet → keep the caller's named model, resolve effort
  from the family default via `normalize_reasoning` (mirrors `served_substitute` leaving the id unchanged,
  `adapters.py:443`). Honor `models.ineffective(model,intent)` (`models.py:202`), `no_substitution`
  (`adapters.py:388`), and vendor-critical pins.
- **Reconcile with the bandit:** the auto-selector sits in `call` (upstream); the utilisation bandit
  (`route_decision`, `adapters.py:1596`) sits in `_call_guarded` (downstream). They must not both rewrite
  `model` in one call — auto-selection wins and suppresses the bandit for that call (both already suppressed
  by `no_substitution`).
- **Stamp + record** the decision (Phase 4).
- **Guard:** `tests/test_auto_select.py` — picks cheapest-at-bar; degrades to caller's model with no
  evidence; respects `no_substitution` and `ineffective`; stays on the $0 path (no LLM) when the frontier
  is decisive.
- **Spend:** $0 in the decisive case; a bounded meta-caged call only on explicit escalation.

### Phase 2 — Bake-off sweeps effort per model + LEARNS (Pillar A, explore)

`bakeoff.bakeoff()` (`bakeoff.py:88`) tests each candidate at ONE default effort and records effort-blind.
Extend it to the user's ask: *per model, use one-or-many reasoning to find the best, and keep it as learning.*

- Add `efforts=None` (ordinal tiers, drawn from what the endpoint VERIFIABLY accepts via
  `vendor_call.discover_efforts`). Turn the candidate loop (`bakeoff.py:122`) into a cross-product
  **(candidate × effort × prompt)**; thread `reasoning=eff` into the fan call (`bakeoff.py:126`).
- Update `_plan` (`bakeoff.py:63`) to multiply the estimate over efforts (estimate-first, budget-capped —
  existing rails).
- **Judge:** reuse `bakeoff._judge_one` with a **pinned** `judge_model` (`bakeoff.py:46`) so every effort
  arm is judged by one ruler (comparability + stable `instrument_id`). Do NOT use `equivalence.grade` here
  (it needs a reference; a bake-off of untried arms has none).
- **Learn:** per model, pick argmin `per_good` across its effort arms (tie-break to the lower effort on
  `models._EFFORT_LADDER`); write `models.add_fact(model, f"effort:{intent}", best_effort, source="bakeoff")`
  (mirrors the `mark_ineffective` convention). Records land per-`(intent, model, effort)` in `calls`
  (effort-aware from Phase 0) and in the measurement receipt with effort in the candidate label
  (`vendor:model@high`) so `advise`/`recommend`/the auto-selector read it back.
- **Guard:** `tests/test_bakeoff_effort_sweep.py` — sweeps the ladder; records per-effort rows; picks
  cheapest-that-holds; writes the fact; estimate scales with efforts.
- **Spend:** real metered calls — estimate-first, refuse over `budget_usd` (unchanged rails).

### Phase 3 — Panels APPLY the learnings, without collapse (Pillar A, panels)

No panel reads advisor/models facts today; effort is left default and slates are hardcoded
(honestreview `PANEL` 4-tuple `repo_review_panel.py:35`, SAGA dict, crossllm `ask`, `validate_findings`).

- **Plumbing:** give `vendor_call.fan_out` (`vendor_call.py:1093`) and `first_ok` (`vendor_call.py:1125`)
  a per-member reasoning (accept `(vendor, model, effort)` triples or a `reasoning_for` callback); `call()`
  already carries `reasoning=` and resolves it per-model at `_call_once` (`adapters.py:1301`).
  `validate_findings` needs no plumbing change (its `vc.call` already carries `reasoning`) — smallest lift,
  do it first.
- **Apply:** each panel sets a member's effort from the learned `effort:{intent}` fact
  (`repo_review_panel.resolve_panel` `:59`; `validate_findings` slate `:32/:43`; `crossllm.ask` `_parse_vendors`;
  `ask_vision` via a `reasoning_for` hook on `bulk_delegate`).
- **Diversity is the hard constraint:** apply the learned `(model,effort)` **within each vendor slot** —
  NEVER a global argmax across members (that IS the bandit collapse). Keep `no_substitution=True` on every
  tuned panel call; a model swap is allowed only within the same vendor slot or to a vendor not already in
  the panel; `panel_integrity.require_diverse` (`panel_integrity.py:73`) must still pass post-hoc.
- **Guard:** `tests/test_panel_applies_learning.py` — members get per-intent efforts; a learning that would
  converge two slots to one vendor is REFUSED (fails closed, `PanelCollapse`); `panel_providers` count
  preserved.
- **Spend:** $0 (wiring + a collapse-attempt test that must raise).

### Phase 4 — Prove the value (Pillar B)

- **Per-decision record.** Add a `decisions` table `(ts, day, project, intent, requested_model,
  requested_effort, chosen_model, chosen_effort, counterfactual_usd, actual_usd, saved_usd, quality,
  quality_conf)`, written at the existing counterfactual seam `adapters._maybe_credit_advisor`
  (`adapters.py:329`, called `:453`) — it already holds `requested` + the result dict (chosen model, cost,
  tokens); Phase 0 adds the efforts. Quality ties back through `calls.feedback`/`callio` on the recorded
  call id (the same signal `advise.ranked` uses).
- **Surface it.** Total `saved_usd` in `receipt._saved_lines` (`receipt.py:898`); expose it in the MCP
  `_tool_spend_overview` (`mcp_server.py:273`, currently real+est-value only); add it to `report.py`; roll
  up via `saas._guarded_rows` (`saas.py:377`). Closes both "no per-decision record" AND "savings invisible
  past the receipt."
- **Invariant:** the saving is the **third axis** — `guard.record_saving` already refuses `source="plan"`
  (`guard.py:24,44`) so a plan-served routing win (already on est-value) can't be double-booked. Certain
  (cache·block·cascade·realized) vs counterfactual (advisor·compaction·**auto-select**) stays labelled.
- **Guard:** `tests/test_decision_savings.py` — a substitution writes one decision row with the right
  counterfactual; a plan-served ($0) win is NOT booked as a saving; the three axes never sum.
- **Spend:** $0.

---

## Part III — Additional extensions (resolve the rest end-to-end)

These are not the core feature but the estate review shows they close the remaining recurring pain. Each is
independently shippable after Phase 0.

- **Auto `max_tokens` from measured need** (challenge #3, EVERY repo). Size the ceiling from measured
  p90/p99 output per `(model, intent)`, reasoning-aware (compose with the self-heal floor), and **respect a
  caller's explicit ceiling** — fixing BOTH the default-4000-too-low (7thsense) and autotune-blew-past
  (cothinking gateway) sides. Deletes the most-duplicated scar in the estate.
- **Auto-slate per intent** (pattern 1). Emit a panel's diverse slate from measured cross-vendor quality
  instead of a hardcoded tuple, carrying first-class *vendor-critical / never-substitute* semantics so it
  never recreates the collapse. Lets warden/honestreview/SAGA stop hand-maintaining slates + guards.
- **Widen the funnel** (secondary pattern). Raw-SDK callers (lmm icd judges, cothinking live gateway, SAGA
  vision) carry no intent → invisible to the advisor. The learnings only bite where calls flow through
  `adapters`/`ask`/`spendrails`; adding intents to those callers is the prerequisite. spendguard's job is
  to make the gated path the path of least resistance (and to keep flagging ungoverned spend, #12).
- **Surface savings in the MCP + report** (folded into Phase 4, called out here as the value-proof that
  consumers repeatedly ask for: "prove what you saved me").

---

## Invariants (non-negotiable — every phase asserts them)

1. **Three axes, never summed:** Real $ (API + Subscription + Remote) · Est-value (plan-covered) ·
   Guarded savings. The savings axis obeys the same rule; `record_saving` refuses `source="plan"`.
2. **Panel diversity never collapses:** learnings apply within a vendor slot; `no_substitution` on tuned
   panel calls; `require_diverse` verified from RESULTS (who answered), never from requested labels.
3. **Decisions about MEANING are agentic:** quality is judged by an LLM (`_judge_one`); the cheapest-at-bar
   pick is arithmetic on a DEFINED scale (efforts, $/good) — parsing, not judgement. Never a keyword/regex
   proxy for quality.
4. **Estimate-first, under the gate:** any effort sweep estimates zero-spend, caps on `budget_usd`, runs
   under `spendguard.require()`. Never de-agentic-ify to save money; cost is controlled by the rails.
5. **Fail loud, never silent:** no-evidence → keep the caller's model and say so; empty/truncated →
   TRUNCATED, not "no findings"; a hung lane → `deadline_exceeded`.
6. **Ground truth:** Σ saved ≤ (counterfactual − actual) at held quality; per-intent $/good cross-checked
   against provider totals, never rigged fixtures.

## Naming (pending the owner's confirmation)

**CONFIRMED: `reasoning="best-value"`** — the value a caller passes to `reasoning=` meaning "delegate: pick
the cheapest model+effort that meets this intent's measured quality." Chosen over `"optimize"` and `"auto"`
(the latter collides with a vendor's native `"auto"` effort, e.g. `kimi-k3='auto'`) for its explicit
cost@quality framing. It is NOT an effort ordinal (`none…max`) and NOT a model id — it is a resolution
directive intercepted at the `adapters.call` chokepoint. An optional quality target rides alongside
(default = the intent's own measured bar).

## Supersedes

Absorbs and replaces the effort-only chip **task_d226a5e1** ("Auto-titrate reasoning effort per intent") —
this is the joint (model + effort) version, delegated at the call chokepoint, learned in bake-offs, applied
in panels, and booked as a provable saving.

## Build outcome (2026-09-10) — what shipped, and the design refinements made in flight

**Shipped + guard-tested (full offline suite green):**
- **Phase 0** — `effort` column on `calls` (+ migration); threaded through `record_call`/`insert`, the gate
  (`_record_rt` reads `kw['reasoning_effort']`), the subscription-lane and bandit writers; `advise.evidence`/
  `ranked(by_effort=True)` slice `$/good` per (intent, model, effort). Guard: `test_effort_on_frontier.py`.
- **Phase 1** — `best_value.select()` (renamed from `resolve` — collided with `conv.resolve`) + `reasoning=
  "best-value"` at the `adapters.call` chokepoint: sentinel consumed, model+effort resolved from the frontier
  ($0, no LLM), pinned so the bandit can't re-swap, honest degrade, no silent discard. Guard:
  `test_best_value_select.py`.
- **Phase 2** — `bakeoff(efforts=[…])` sweeps (candidate × effort × prompt), records per-(intent,model,effort),
  summarises the cheapest holding effort per model. Guard: `test_bakeoff_effort_sweep.py`.
- **Phase 4** — `guard.record_decision`/`decisions_since`/`decisions_summary` (a per-decision `decisions`
  table); `adapters._book_substitution` (was `_maybe_credit_advisor`) prices the counterfactual off
  `substituted_from` and books both the decision and the saving; `mcp_server.spend_overview` exposes the
  saved third axis. Guard: `test_decision_savings.py`.

**Refinements vs the plan (each an improvement, made honestly):**
- **No second fact store.** The plan mentioned a `models.add_fact(effort:{intent})`; the repo's own
  `vendor_call.py` doctrine warns "two stores for one fact is worse than either." The per-(intent,model,effort)
  rows the bake-off writes into `calls` ARE the learning that `advise`/`best_value` read back — so no separate
  effort fact was added.
- **`call_io` effort deferred.** The frontier reads `calls`, not `call_io` (a prompt-sampling store), so the
  effort column there adds nothing to the selection loop — not built.
- **best-value savings source** is registered as a counterfactual (`guard.CONFIDENCE['best-value']`), kept
  apart from the CERTAIN (measured) sources; `record_saving` still refuses `plan`.

## Update — agentic best-value + reasoning-effort auto-titration (2026-09-11)

Two decisions from the owner reshaped the selection layer, and both are now built + guard-tested (full offline
suite green):

**1. The selection is AGENTIC (the owner's call, and the agentic-decisions doctrine's).** The first cut of
`best_value.select` picked model+effort by arithmetic ($/good, a min-sample cutoff, a bar) — the pre-commit
`agentic_decisions` hook blocked it, rightly: "does 89% hold vs 90%?" and "is 1.99 labels enough?" are
meaning-judgements at the margin. `best_value` now DELEGATES both axes to the learnings, each decided the right
way, and makes NO arithmetic cut itself:
  - the **MODEL** is chosen by the advisor's agentic ranker (`advisor.recommend_models` — meta-caged,
    estimate-first LLM over the measured frontier);
  - the **EFFORT** rides the titration fact (below);
  - `pin_model=True` (a panel member) keeps the model and only supplies its learned effort — diversity by
    construction; a cold intent → `model=None` (keep the named model). A deliberate stop propagates.
  Follow-up (noted, not built): cache the per-intent model verdict (evidence-fingerprint invalidation) so the
  agentic pick amortizes to ~one call per intent rather than per call. Guard: `tests/test_best_value_select.py`.

**2. Reasoning-effort AUTO-TITRATION per (intent, model)** — the effort twin of the token-budget self-heal, and
the layer that lets `best_value` shed its arithmetic. New `effort_titration.py`:
  - A/Bs the effort ladder (minimal→low→medium→high) on a sample of the intent's real prompts; the judge scores
    each output **1–10** (graded, not a good/bad bit); an **agentic verdict** over the per-effort score table
    names the cheapest effort that HOLDS quality + a confidence — no hand-picked margin.
  - Records it as the per-(intent,model) fact `models.record_effort(model, 'effort:<intent>', …)` (a DERIVED
    DECISION, the sanctioned `models.py`-facts-applied-by-the-chokepoint pattern — not a second copy of the
    evidence), read back by `models.effort_for`.
  - **Auto-applied at the chokepoint** (`adapters._call_guarded`, beside the reasoning-budget floor): a call on a
    titrated intent with NO explicit effort gets the learned effort; an explicit effort always wins; an
    unmeasured intent falls through to the FAMILY FLOOR (never force 'high' everywhere).
  - INCREMENTAL + RESUMABLE (chunk-never-single-shot): expands a chunk at a time, takes the agentic verdict after
    each, stops when confident, checkpoints after EVERY measured prompt (per-effort cursor + failed-index list),
    so a crash resumes exactly and never re-pays; deadline-bounded meta calls, per-prompt size guard,
    estimate-first, meta-capped. CLI `spendguard effort-titrate <intent>`. Guard:
    `tests/test_effort_titration.py`.

**Clean separation this created:** effort-titration OWNS the effort axis (learned per (intent,model), applied
implicitly to every call); `best_value` OWNS the model axis (agentic) and rides the effort fact. The bake-off
(Phase 2) stays the broad (model×effort) EXPLORER that fills the frontier; the per-(intent,model) effort VERDICT
is titration's, not the bake-off's (the arithmetic `_learned_efforts` summary was removed).

---

**Phase 3 — partially shipped, one wire pending (honest gap):**
- **Done:** the substantive capability — per-member best-value with **diversity preserved** — works through the
  DIRECT-call path: a panel member calls `vendor_call.call(reasoning="best-value")`, which already sets
  `no_substitution=True`, so `adapters.call` (Phase 1) resolves with `pin_model=True` — titrating the member's
  own effort, never swapping its model (the collapse the pin prevents, proven in the test). Guard:
  `test_panel_applies_learning.py`.
- **Pending:** forwarding `reasoning` through `vendor_call.fan_out`/`first_ok` (and `crossllm.ask`) so
  CONSENSUS-fan panels (not just direct-call ones) get it too. This is a ~3-line change blocked at authorship by
  the `durability` PreToolUse guard — a FALSE POSITIVE here (adding an effort kwarg does not change `fan_out`'s
  realtime, non-resumable durability; the durable path is `bulk_delegate`). It applies cleanly with
  `DURABILITY_ALLOW=1` set for the write. Left unapplied rather than bypassing a standing guard unattended.

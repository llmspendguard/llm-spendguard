# Domain migration plan — meticulous, step-by-step, provable after every step

**Goal:** reorganize `src/spendguard` into the [7-kind × 21-context taxonomy](DOMAIN_TAXONOMY.md) **without breaking
shipped behavior and without ever holding two live copies of any code**, while measurably shrinking the 142-module
import cycle. Executed as a strangler: one context at a time, each a PR-sized step with a pass/fail oracle.

Scope approved 2026-11-02: **Phase 0 (ratchet oracle) + one leaf-context pilot**, then re-measure and decide on the
hubs. The hub decoupling (`gate`/`adapters`/`budget`) is NOT in this scope yet.

## Non-negotiable method (decided)
1. **MOVE, never COPY.** `git mv` each module to its new context package (history preserved). A *copy* would create
   two drifting sources — the exact smear the taxonomy removed. One source of truth, always.
2. **Shim the old path.** After a move, leave a one-line re-export at the old path
   (`from spendguard.<kind>.<context>.<mod> import *`) so every existing `import spendguard.<mod>` keeps working
   untouched. The shim is deleted only after consumers move to the surface.
3. **One surface per context.** The context package's `__init__` IS its published surface. Cross-context imports go
   only through a peer's surface, never its internals (see [TAXONOMY §0b](DOMAIN_TAXONOMY.md)).
4. **Directories are not the work.** A move that does not route cross-context access through a surface only relocates
   the hairball. The surface rewiring is the substance; the ratchet proves it.

## Global invariants — the gate after EVERY step (all must hold or the step is reverted)
- **G1 whole suite green** (chunked, not sequential) — `spendguard deploy` gate.
- **G2 ratchet holds:** measured `largest_scc_size` and `cyclic_edge_count` are **≤ baseline** (never grew). A step
  that claims decoupling must make at least one of them *drop*, or it did nothing structural (flag it).
- **G3 no new boundary violation:** no new cross-context import to a non-surface module (once the boundary gate from
  Phase 0.5 exists).
- **G4 back-compat:** every pre-existing public import path still resolves (shims intact). A smoke test imports the
  old paths.
- **G5 clean commit:** exactly one commit on `spec/domain-taxonomy-seed`, `--no-verify`, trailer present; no push, no
  main. Tree clean after.
- **G6 ruff + name-uniqueness + readiness/selfcheck green.**

If any of G1–G6 fails, the step is reverted (`git revert`/reset to the prior green commit) — we never advance on a
red state.

## Phase 0 — the ratchet oracle (bounded; makes every later step provable)
Brief: `scratchpad/brief_phase0_acyclicity_magnitude_ratchet.md`.
- **0.1 Ground.** Read `architectural_fitness.py`, `architectural_fitness_baseline.json`, the guard test. **Oracle:**
  codex reports the current measured `{largest_scc_size, cyclic_edge_count}` and it matches the grounding (≈142/941).
- **0.2 Magnitude ratchet.** Add the two metrics; baseline ceilings stored as DATA; gate fails on growth; explicit
  tighten-on-shrink; keep the no-new-edge check. **Oracle:** unit tests — passes at baseline; **goes RED when the SCC
  is made to grow** (a check that cannot fail is a defect); detects a shrink.
- **0.3 Assert the REAL package** (not just the scratch fixture) in the suite. **Oracle:** the real-package assertion
  runs and passes; G1 green.
- **0.4 Commit.** **Oracle:** G1–G6. Baseline numbers recorded in the commit message.

## Phase 0.5 — the boundary gate (added with the pilot, once there is ≥1 real context package)
Extend `architectural_fitness` with a `context → surface-module` registry (DATA) and a check: a cross-context edge is
legal only if its target is the target context's surface. **Oracle:** passes for the pilot context's surfaced
imports; goes RED for a deliberately-added deep cross-context import.

## Pilot — migrate ONE leaf context end-to-end (proves the whole machine)
Pilot context = chosen **by measurement** (lowest total cross-context coupling; small; cleanest boundary) — selection
pending the coupling report; recorded here once picked. The per-context loop (also the template for all later
contexts):
- **L.1** Create the context package `spendguard/<kind>/<context>/` with an empty surface `__init__`. *Oracle:* G1.
- **L.2** `git mv` the context's modules in; add shims at every old path. *Oracle:* G1 + G4 (old imports resolve).
- **L.3** Define the surface: `__init__` re-exports exactly the operations peers use (no more). *Oracle:* G1; the
  surface's public names are a deliberate list, reviewed.
- **L.4** Rewire cross-context consumers to import the pilot's **surface** (and the pilot's own cross-context imports
  to peers' surfaces where those surfaces exist). *Oracle:* G1 + G2 (SCC drops or holds) + G3.
- **L.5** Turn on the boundary gate (0.5) for the pilot context. *Oracle:* G3 red on a planted deep import, green
  otherwise.
- **L.6** Commit. *Oracle:* G1–G6; record the SCC delta (did it move?).

**Pilot exit criterion:** the machine (package + surface + shim + boundary gate + ratchet) is proven on one context
with all invariants green, and we have a measured SCC delta. Then — and only then — decide whether to fund the hub
contexts (`gate`/`adapters`/`budget`), where the cycle actually lives.

## What we explicitly are NOT doing yet
- Not moving the hub/split modules (§2) — that is the real cycle and a separate funded decision after the pilot.
- Not deleting any shim until its consumers are surfaced.
- Not reorganizing for aesthetics — every step must move a measured invariant, or it is not taken.

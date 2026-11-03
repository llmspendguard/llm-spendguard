# Domain migration plan — reach the 7×21 STRUCTURE safely; make acyclicity a separate, funded program

**Goal:** get `src/spendguard` into the [7-kind × 21-context taxonomy](DOMAIN_TAXONOMY.md) — navigable packages, one
published surface per context, a home for new code, boundaries enforced — **without a money-safety regression**, and
measurably **containing** the import cycle. This is driven by a cross-checked decision (2026-11-02), not a hunch.

## The decision and why (3 independent reviews + deterministic measurement, unanimous)

Reviewers (codex, a fresh claude.ai, and in-repo AST re-measurement) unanimously rejected the original "migrate one
leaf context as a pilot to prove the machine" approach, because measurement falsified its premise:

- The import graph is **one 142-module SCC / 943 cyclic edges** — a dense within-layer **mesh** through the hubs
  `gate`/`adapters`/`budget`, not a ring and not a layering inversion. **No cheap first move exists:** every candidate
  (Work Integrity's 3 back-edges; reversing `config→ledger_*`) measures only ≈142→140. Breaking it is a sustained
  hub-decoupling program, not an edge-cut.
- A leaf pilot proves only the plumbing (`git mv` + shim + surface) and teaches nothing about the hard part.
- `import spendguard` installs the spend gate **as an import side-effect**. A careless reorder that changes import
  order could silently **ungate spending** — a money-safety bug in a money-safety product. This asymmetric downside,
  against a maintainability-only upside, is decisive.

**What the decision does NOT mean.** It forbids a pointless pilot and *blind acyclicity refactoring in money code*. It
does **not** forbid the 7×21 structure. The structure and acyclicity are **two separable deliverables**:

| Deliverable | What it is | Character |
|---|---|---|
| **STRUCTURE (7×21)** | modules in context packages, one surface each, new code has a home, boundaries enforced | **mechanical & safe** once import-timing is proven invariant — the mesh may persist *inside* the clean structure |
| **ACYCLICITY** | breaking the 142-SCC | hard, hub-centric, multi-week, money-safety-sensitive — a separate, progressively-funded program |

So: deliver the STRUCTURE (Phases 1–3) safely now; make ACYCLICITY (Phase 4) a separate effort the structure enables.

## Non-negotiable method (updated by the reviews)
1. **MOVE, never COPY** (`git mv`; history preserved; one source of truth).
2. **Shim via `sys.modules` aliasing, NOT `from … import *`.** A wildcard re-export creates a *second* module object,
   so a test that patches the old path patches a copy and passes falsely. The shim aliases the real module object and
   emits a `DeprecationWarning`.
3. **One surface per context, lazy.** The context package `__init__` publishes an explicit `__all__` resolved lazily
   via PEP 562 `__getattr__`. A test asserts every exported name resolves and none is private (`_`-prefixed).
   Cross-context imports go only through a peer's surface.
4. **Boundary gate BEFORE any move** (not during). Declared compatibility shims are explicitly exempt.
5. **Behavioral equivalence is proven per step** (Phase 1 harness), not assumed. Import-resolution smoke tests are not
   enough — the gate-install timing must be identical.
6. **Directories are not the work.** A move that does not route cross-context access through a surface only relocates
   the hairball.

## Global gate — after EVERY step in Phases 2–4 (all hold or the step is reverted)
- **G1** whole suite green (chunked).
- **G2** ratchet holds on the **shim-collapsed logical graph** (`largest_scc_size`, `cyclic_edge_count` ≤ baseline);
  the **physical graph** is reported alongside; a decoupling step must make a metric DROP or it did nothing. A second
  ratchet: the shim count and imports-through-shims may only go DOWN.
- **G3** no new cross-context import to a non-surface module (boundary gate).
- **G3b** the ratchet graph includes **dynamic** imports of `spendguard.*` (`importlib.import_module`/`__import__`
  with string targets); non-literal dynamic imports of package modules are a reviewable violation (closes the AST
  blind spot).
- **G4** behavioral equivalence: the Phase-1 golden import-snapshot is reproduced exactly, including *first SDK call is
  gated* in every permutation.
- **G5** every pre-existing public import path still resolves (shims intact).
- **G6** one clean commit on the spec branch, `--no-verify`, trailer; no push, no main.
- **G7** ruff + name-uniqueness + readiness/selfcheck green (no NEW failures vs the pre-step baseline).

## Phases

### Phase 0 — honest magnitude ratchet — DONE (`7ff763dc`)
Measures the real package; records `max_largest_scc_size=142`, `max_cyclic_edge_count=943` as DATA ceilings; fails if
either grows; tightens down on shrink; ships a test proving the gate goes RED on growth for each metric independently.
Whole suite (815 files) green with it.

### Phase 1 — the safety unlock: behavioral-equivalence harness (do FIRST; blocks all moves)
Build a golden import-snapshot test (claude.ai's design): for each entry point — `import spendguard`,
`python -m spendguard`, the sitecustomize hook, `import openai` before `spendguard`, and importing one submodule alone
— run a **fresh subprocess** and record, via a `sys.meta_path` finder (old paths mapped to new names): the module
execution order; whether the SDK methods are wrapped; gate-installed state; `atexit`/hook registrations; logging
handlers. **Oracle:** the snapshot is reproducible, and one hard assertion holds in every permutation — *the first SDK
call is gated.* Until this is green, **no module moves.** If a later move breaks the snapshot, either fix the move or
make gate-install order-independent/idempotent — never weaken the assertion.

### Phase 2 — scaffolding (purely additive; zero risk to shipped code)
- **2a** the empty 7×21 package skeleton + lazy PEP-562 `__all__` surfaces (no modules moved yet).
- **2b** the boundary-gate: a `context → surface-module` registry (DATA) + a check (G3), with shim exemption.
- **2c** the `sys.modules`-alias shim mechanism + its DeprecationWarning + the "exports resolve / none private" test.
- **2d** ratchet upgrades: shim-collapsed logical graph + physical report (G2); shim-count-only-down ratchet;
  dynamic-import inclusion (G3b).
- **2e** **new-code rule ON:** new modules must live in a context package and import only surfaces (strangler for NEW
  code only — no existing module moves). Enforced by the boundary gate.

### Phase 3 — structural migration (mechanical, context-by-context) — delivers the 7×21 STRUCTURE
For each of the 21 contexts (leaves first for low blast radius, hubs last): `git mv` its modules into the package +
`sys.modules` shim at every old path + register its surface + rewire **new/touched** cross-context imports to peer
surfaces. **Oracle:** G1–G7 after each context. The SCC is expected to stay ~142 throughout — that is correct;
**structure is the deliverable here, not acyclicity.** Exit: all 220 modules in context packages, every surface
published, boundary gate enforcing, behavioral snapshot unchanged.

### Phase 4 — acyclicity, progressive & degree-ranked (separate, funded effort)
Attack the mesh hub-by-hub `gate`→`adapters`→`budget`, each a within-mesh dependency inversion proven by the ratchet
**dropping** (G2). Funded incrementally; the structure (Phase 3) makes each hub's responsibilities explicit and its
surface the inversion point. **Re-open/escalate triggers:** an import-order bug surfaces; a request to embed one
context in another product; a cold-import-time budget is exceeded.

## Measured taxonomy corrections (ground truth, fold into [DOMAIN_TAXONOMY.md](DOMAIN_TAXONOMY.md))
- **`config` is NOT L0.** It imports `adapters`, `codex_exec`, `content_compression`, `pricing`, and 5× `ledger_*`
  (`ledger_fork_locks`, `ledger_read`, `ledger_request_cursor`, `ledger_wal_maintenance`, `ledger_writer`). It is deep
  in the mesh; reversing its upward edges alone yields only 142→140.
- **`execution_journal ↔ delegate_jobs` are mutual** — `execution_journal` belongs in **Token Operations**, not Work
  Integrity. Fix its placement in the taxonomy.
- **No cheap cycle-break exists** anywhere (every candidate ≈142→140), confirming acyclicity is a sustained hub
  program, not an edge-cut.

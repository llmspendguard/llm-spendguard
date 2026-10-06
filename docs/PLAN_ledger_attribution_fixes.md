# PLAN — ledger attribution + batch-booking fixes (reported by the LMM-viz session)

Four accounting-correctness fixes. Work on branch `ledger-attribution-fixes`, make **ONE** commit at the end (the
honestreview pre-commit gate is costly per commit), run the full gate green, do NOT push / no PR. Obey CLAUDE.md hard
rules (no hardcoding, decisions agentic not regex, read files whole, unique semantic names, no shortcuts, split
billed-$ from est-value and never sum them, offline tests set their own fake key). The ledger is billing-critical:
every schema change is ADDITIVE (new columns NULL on legacy rows), never destructive; never clamp/delete existing data.

Context: a heavy real workload could not tell, from the `calls` ledger, whether $0-lane rows under a pinned
`metered_only=True` fan were (a) its own votes, (b) spendguard's own gate-internal meta calls, or (c) another
session's work — because all three inherit the same `intent` via `calls.set_context`. That ambiguity produced two
retracted findings in one day. Fixing #1 makes the other three answerable/auditable.

## #1 — attribution dimensions on the `calls` ledger  (the root cause; do this first)
Files: `calls.py` (schema in `_ensure_calls_schema`; `set_context` L49; `_resolve_attribution` L313; `record_call`
L376 + the `insert` writer L500 — BOTH INSERT paths must stay in step, they already share `_resolve_attribution`).

Add **two** additive columns to the `calls` table (NULL on legacy rows), threaded through `set_context` (new optional
kwargs), `_resolve_attribution`, and BOTH INSERT sites:
- **`call_class`** — one of `workload` | `gate_internal` | `probe`. Default when unset = `workload` (a caller's own
  call). spendguard's OWN meta LLM calls must stamp `gate_internal` **even when a workload `intent` is in context** —
  that is the whole point (today a classify/route/`expected_output.expect`/advisor/bakeoff call made inside a
  workload `set_context` inherits the workload intent and is invisible as gate-internal). A tiny probe stamps `probe`.
  Mechanism is yours, but it must cover ALL gate-internal meta-call sites without per-call opt-in drift — prefer a
  single `calls.gate_internal()` context-manager (sets `call_class='gate_internal'` on the thread-local ctx, save/
  restore like `fell_from_context`) wrapped at spendguard's meta-dispatch entry points (the advisor/classify/
  expected-output/routing/bakeoff calls, incl. `delegate_router.classify_task`), rather than editing every call. If a
  meta call already pins a `spendguard:*` intent, still stamp `call_class` so the dimension is uniform.
- **`origin_session`** — a stable per-PROCESS id (a module-level `uuid4().hex[:12]` created at first import of
  `calls`, reused for the process; NOT `config.machine_id()`, which is machine-level). Stamped on every row so rows
  from DIFFERENT concurrent sessions/processes under the SAME intent are separable. No PII.

Surface them where spend is read back: the per-(executor,model,intent) breakdown the report used (reconcile / the
`spend`/`calls` views) must be groupable + filterable by `call_class` and `origin_session`, so "MY session's workload
votes under intent X" is one query. Keep the existing output columns; add these as available dimensions, don't break
callers.

## #2 — audit `metered_only` for a lane leak (enabled by #1)
`metered_only=True` is contracted to SKIP the $0 lane and force the metered API (`adapters.call` L764; `bulk_delegate`
L401 / `_run_task_on_api` L898). Audit `adapters.call` → `_call_once` and `bulk_delegate` end-to-end for ANY path
where `metered_only=True` can still resolve to a lane executor. Add an offline test asserting a `metered_only=True`
call (single and via `bulk_delegate`) records **executor = NULL (metered api), never a lane** — on the happy path AND
on a lane-down/fallback path. Then, using #1's `call_class`/`origin_session`, state whether the reported lane rows
under the pinned intents are gate_internal / another session (expected) or a genuine `metered_only` leak (a defect) —
and if a leak exists, fix it. Record the conclusion in the commit message; do not claim a leak you cannot show.

## #3 — batch booking: no double-book on refusal, reconcile estimate→actual on collect
Files: `submit.py` (the submit-time provisional booking + `guarded_submit` L238 / `submit_message_batch` L430) and the
gate's provisional batch-cost row + the `collect`/settle path.
- A **refused** submit must NOT leave a booked provisional cost row (roll back / never book when the gate refuses).
  Concrete case from the report: a refused submit booked ALONGSIDE the real one — a $12.77 double-book
  (`d095de2a850445e6` / `d152ba0ba5884d51`). Guard: a refused submit books zero rows.
- **`collect` must reconcile** the submit-time ESTIMATE row to the measured ACTUAL usage when results are collected
  (replace the provisional estimate cost/tokens with the actual), so the ledger converges to truth without waiting for
  a separate `reconcile` pass. Today provisional estimates over-state until the owner reconcile trues them down
  post-hoc; collect is where the actuals first exist, so reconcile there.
- Offline tests: refused submit → 0 booked rows; collect → the provisional row's cost/tokens become the actuals.

## #4 — CLI `overage` command (parity with the MCP tool)
`cli.py` (_GROUPS L24+ and `_dispatch` L124). The overage check exists only as the MCP tool
`spendguard_overage_status`; there is no CLI command, and `spendguard overage-status` fuzzy-suggests the UNRELATED
`coverage` (which means "which interpreters are ungated"). Add a `overage` CLI command (a thin wrapper over the SAME
function the MCP tool calls — find and reuse it, do not reimplement the check), registered in `_GROUPS` + `_dispatch`,
unique name, with a one-line help. Keep the split-cost display convention in its output.

## Verification (ONE commit, all green)
- `spendguard deploy` gate: ruff + chunked pytest (475+ files) + name-uniqueness + `spendguard audit --ci`.
- New offline tests above pass; schema migration is additive and a legacy DB (no new columns) still opens + records.
- Commit on `ledger-attribution-fixes` with a message stating the #2 conclusion (leak or not, with the evidence).
  Do NOT push / no PR — leave the branch for review.

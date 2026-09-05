# Measurement receipts — a reproducible, comparable record of a judged number

A quality number (a bakeoff's `good_rate`, an eval score) is only meaningful if you know **what produced it**. Today
spendguard emits a **spend** receipt (what the money bought); this is its twin — a **measurement** receipt (what a
number *means*), so a user can (1) see the judge mix behind a score, (2) re-run *the same instrument* for a comparable
number, and (3) be told, loudly, when the instrument drifted.

This is the `RECONSTRUCT → ATTRIBUTE → TALLY → AUDIT → FLAG` identity applied to a measurement instead of a dollar.

## The problem it closes (grounded)

`bakeoff()` judges each candidate output with `judge_model = config.advisor_judge_model()` (a single global model,
resolved fresh every run) and records `quality` per `(intent, model)` with `who="bakeoff"`. But the judge model is
**return-only** (`judged_by=` in the result) — it is never persisted onto the recorded rows. The sample is
auto-drawn and not pinned; the rubric (`_JUDGE_SYS`/`_JUDGE_SCHEMA`) is versioned only by the code. So:

- You cannot ask, later, **which judge produced 0.88** — the instrument was discarded.
- Next month's number uses whatever `config.advisor_judge_model()` resolves to *then*. **0.88 (Aug) and 0.85 (Sep)
  can be different rulers**, and nothing says so.

## The third category: `measurement-stable`

A model can be pinned for three different reasons — and they are orthogonal:

| category | pinned… | for | mechanism |
|---|---|---|---|
| **fungible** | for nothing | cost | bandit-swappable |
| **vendor-critical** | *within* one measurement | cross-vendor integrity NOW (a panel needs N distinct vendors this run) | `no_substitution=True` |
| **measurement-stable** | *across* measurements | the same ruler over time | pin-to-receipt (this doc) |

A single tracked judge is measurement-stable but not a panel; a monthly consensus panel is *both*. So
measurement-stable is a property of the **measurement**, recorded on its receipt — not merely a flag on a model.

## Two ids

- **`reading_id`** — one specific number with its full provenance ("0.88, `rd_…`, 2026-08-03, judge haiku-4.5").
  Citable; this is what a user references.
- **`instrument_id`** — the RULER: `sha256(intent + kind + judge_mix + aggregation + sample_hash + rubric_hash +
  sorted(candidates))`. **Two readings with the same `instrument_id` are comparable.** It deliberately excludes the
  value and the date (those are the result, not the identity).

A re-run "as per the receipt" reproduces the reading's `instrument_id` → a new reading, same instrument, a `parent`
link (a lineage / time series). A re-run with a changed judge, sample, or rubric produces a **different**
`instrument_id` → not comparable → flagged.

## The receipt (one reading)

Stored in a `measurements` table in the ledger db (`~/.spendguard/spend.db`):

| field | why |
|---|---|
| `reading_id`, `instrument_id`, `parent_id` | identity + lineage |
| `ts`, `intent`, `kind` (bakeoff / eval / …) | provenance |
| `judge_mix` | model(s) **as ACTUALLY served** (`adapters.served_by` / `panel_providers`), not intended — a collapsed panel can't masquerade |
| `judge_pinned` (bool) + `judge_version_basis` | was the judge pinned to a snapshot id, or best-effort on a vendor that self-updates |
| `aggregation` | majority / mean / single |
| `sample_ids` + `sample_hash` + `n` | which items; pins the sample so a same-sample rerun is comparable |
| `rubric` + `rubric_hash` | the judging prompt + schema (`_JUDGE_SYS`/`_JUDGE_SCHEMA`), hashed into the identity |
| `candidates` | what was measured |
| `values` | good_rate (± CI, see below) per candidate |
| `spend_usd`, `pricing_snapshot`, `spendguard_version` | cost + environment |

## Three flows

- **inspect** — `spendguard measurement inspect <reading_id>`: the judge mix, sample, rubric, date, value. For a
  PAST bakeoff run before receipts existed, `inspect` RECONSTRUCTS what it can from the ledger (candidates,
  good_rate, N, date range) and marks the judge **`unknown` (not stamped before receipts existed)** — an explicit
  INSUFFICIENT marker, never a guessed judge. "Cannot tell" is not "clean."
- **rerun** — `spendguard measurement rerun <reading_id>`: re-execute with the reading's *pinned* judge + *same*
  sample + *same* rubric → a new reading with the SAME `instrument_id` and `parent = <reading_id>`. It re-judges, so
  it SPENDS → estimate-first + `BudgetRefused` before any spend (reuses the bakeoff estimator). Reports a
  **delta with a CI** (below), re-measuring the baseline, not a bare point.
- **re-baseline / drift-flag** — if the pinned judge is unavailable, or the caller *chooses* a new judge/sample/rubric,
  the run produces a different `instrument_id`; spendguard FLAGS it: *"different instrument than `<reading_id>`
  (haiku-4.5 → haiku-5) — NOT comparable to the original; confirm re-baseline"* rather than silently continuing a
  series across a ruler change.

## Subtleties that keep it honest (not just plumbing)

1. **Same judge ≠ same number.** Judges sample at temperature and drift server-side. "Comparable" means *same
   instrument*, not *same output*. So a rerun re-measures the BASELINE too and reports a **delta with a CI** — a
   0.88→0.85 move is flagged *significant or not* against the judge's own measured noise (the `n=` / judge-noise ±
   doctrine). The receipt carries that noise.
2. **Same-sample vs fresh-sample is a deliberate fork.** Same-sample rerun = pure instrument comparability;
   fresh-sample + same judge = tracks the POPULATION. Two different questions; the receipt makes the caller pick,
   never silently one (cf. vision `cache` / `no_cache`).
3. **Rubric drift breaks comparability even on the same model** → the rubric is hashed into `instrument_id`.
4. **Provider version pinning is best-effort.** `gpt-5-nano` may update server-side; where a dated/snapshot id
   exists, pin it and set `judge_pinned=true`; where it doesn't, `judge_pinned=false` and the receipt SAYS
   "comparability is best-effort on this vendor" — never implies a guarantee it can't keep.
5. **Rerun spends** (it re-judges) → estimate-first + budget, always.

## Build order

- **(a)** this doc — the contract.
- **(b)** receipt emission on bakeoff/eval + `spendguard measurement inspect`, incl. RECONSTRUCT-from-ledger for
  past runs (honest `unknown` judge). $0.
- **(c)** `rerun` (estimate-first, delta+CI), the drift-flag/re-baseline, and a `measurement_stable` pin-reason in
  the value-router (a judge tagged for a tracked metric is never bandit-swapped). Spends only on rerun.

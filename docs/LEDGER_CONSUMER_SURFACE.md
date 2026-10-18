# Ledger connection cost + consumer query surface (spec for a delegate run)

Three deliverables, in this order. **Part A is a diagnosis, not a fix** — do not skip it, and do not start
Part B's design until A has produced the actual SQL and the actual cost split. Part D is hygiene, gated on
a verified backup.

Repo doctrine applies in full: `CLAUDE.md` §0 (GROUND BEFORE YOU ACT), §0b (match the unit of review to the
unit of the defect), §anti-amnesia (every lesson ends as a test/lint/assert, never prose), and the spend
rules (run under the gate, estimate before spend).

---

## The observation

On 2026-10-17, `honestreview-mcp --http --port 4912` — a warm daemon that imports the gate — was caught
burning CPU repeatedly. **Four separate daemon instances** were observed in a few hours (PIDs 6602, 8471,
13857, 21127), each with the same shape: **burn hard on startup, then subside**.

Worst instance: **79 CPU-minutes over 75 wall-minutes — ~250% of a core, six threads at ~39% each.**

Measured during the burns:

- **Every busy thread inside SQLite.** `sample` caught 3,064 samples on one identical stack:
  `thread_run → context_run → gen_iternext → builtin_next → pysqlite_connection_execute →
  _pysqlite_query_execute → sqlite3_step`. Generator-driven cursor iteration, CPU-bound (`STAT R`).
- **43 open SQLite connections in the one process** (`lsof`, PID 8471 at 3 minutes old, 154% CPU):

  ```
  36 × ~/.spendguard/spend.db      (+30 -wal, 2 -shm)
   7 × ~/.spendguard/lane_queue.db (+ 4 -wal, 2 -shm)
  ```

- **~3 ledger rows written per minute.** `SELECT COUNT(*) FROM calls` moved 704,613 → 704,614 in 20 s. The
  rows were correct and correctly routed (`honestreview:*` intents on the codex `$0` lane).
- **Zero inbound connections** on :4912 during the worst burn — this was not request traffic.

**250% of a core to write three rows a minute is the defect.** Nothing in the tuning posture explains it.

### What has already been ruled out — do not re-derive these

| hypothesis | verdict | evidence |
|---|---|---|
| the consumer queries `spend.db` directly in its daemon | **NO** | honestreview's only direct openers are `evals/precision_recall.py:39`, `src/honestreview/refute.py:549`, `scripts/honestreview_guard_ledger.py`; none runs in the warm daemon |
| the consumer's 10 s source re-hash | **NO** | measured: 155 files, 2.1 MB, **6 ms/pass = 0.1% of one core** |
| WAL runaway / failed checkpoints | **NO** | `-wal` sits at exactly 67,108,864 B = exactly the `journal_size_limit` this repo sets. The cap is working (it was 1.3 GB on 2026-10-08 before the cap) |
| starved `cache_size` / tiny `journal_size_limit` | **NO — measurement error** | those were read from a *separate* `sqlite3` CLI connection. Both pragmas are **per-connection**; `tune_ledger_connection` sets `cache_size=-64000`, `mmap_size=256MB`, `journal_size_limit=64MB` |
| SQLite being unsuitable | **NO** | it is behaving exactly as configured; the tuning posture is sound and its docstring explains each pragma |

### The structural fact that IS established

`PRAGMA page_count × page_size` = **4.4 GB file**. `dbstat` by table:

```
spend_events 306 MB · calls 282 MB · spend_audit 196 MB · gate_calls 34 MB   ≈ 850 MB live
```

**~3.5 GB of the file is free pages.** Never VACUUMed. Real and file-level — but **not by itself an
explanation for the CPU**. Say so if you cite it.

---

## Part A — attribute the CPU (diagnosis; do this first)

`py-spy` is **not installed anywhere on this machine**, and `sample` only reaches the C stack, which is why
the previous investigation could not get Python frames. Fix that first. **`py-spy` needs `sudo` to attach on
macOS** — if the sandbox denies it, say so verbatim rather than substituting a weaker method.

1. Install `py-spy` into a scratch venv (not a project venv).
2. Reproduce the burn. It is **bursty and self-resolving**, so it must be profiled *inside* a burst — two
   earlier instances exited mid-investigation. Drive a consumer review through a freshly started warm
   daemon and confirm the shape first (`ps -M` showing threads `R` in `sqlite3_step`).
3. `py-spy dump --pid <pid>` for every thread, and `py-spy record` for a flame graph over ≥60 s.

**Then answer with evidence, not reasoning — the central question is a cost split:**

> **What fraction of the CPU is connection setup (connect + 7 PRAGMAs + `_ensure_schema`) versus actual
> query execution?**

The 43 connections and the burst-on-startup shape both point at setup, and so does an asymmetry worth
explaining: **36 `spend.db` fds but only 2 `-shm`**. A WAL-mode connection maps the shared-memory index
when it actually reads, so most of those connections may never have done any work at all — pure setup.
**Confirm or refute that; do not assume it.**

Also quantify:

- How many connections are created per unit of work, and how long does each live?
- Does `_LEDGER_POOL` (`config.py`, thread-local per `(thread, subsystem)`) multiply combinatorially with a
  fan-out thread pool? With ~8 subsystems that is ~8 connections per worker thread.
- Each of the 36 asks for `cache_size=-64000` (64 MB) and `mmap_size=256MB` — a budget of ~2.3 GB page
  cache and ~9 GB mmap address space for one daemon. Measured footprint was 456 MB (peak 917 MB), so the
  caches were **not** full. What is the real resident cost per connection?
- How often does each new `(thread, subsystem)` connection re-run `PRAGMA table_info` / `PRAGMA index_list`?
- Is anything re-reading rows it already read — a poll loop, a freshness check, an un-indexed predicate, or
  a `SELECT *` pulling the large payload columns (4.4 GB / 704 K rows ≈ 6 KB/row)?

**Deliverable:** a written finding naming the mechanism, with the profile attached and a reproduction. If
the burn cannot be reproduced, say so plainly and stop — do not proceed on a guess.

---

## Part B — bound the connection pool

Independent of what Part A finds, **43 connections in one process is not defensible** and should be fixed
on its own merits.

Why it matters more than it looks: a consumer that cold-starts once a week would never notice this. But
the observed consumer was being **actively developed**, and its own staleness check exits the daemon on any
source edit — **13 edits in 2 hours, a mean daemon lifetime of roughly 9 minutes.** Every restart repaid the
full 43-connection warmup. spendguard does not control the restart frequency, but **it does control what a
restart costs**, and that is the half that belongs here. (The consumer is fixing its half separately; see
`honestreview/docs/design/warm_daemon_ledger_consumer_migration.md`.)

Ground first, per §0 — read `_LEDGER_POOL` and the `_X_db()` openers before changing anything, and
understand *why* it is keyed per subsystem (the docstring is explicit: so a nested op across subsystems
cannot commit or roll back another's in-flight transaction). **That constraint is real. Do not collapse to
one global connection and reintroduce the bug the keying was added to prevent.**

Requirements:

- **A hard, configurable ceiling on live connections per process**, with the current count observable.
- **Setup paid once, not per connection** — `_ensure_schema` should not re-run `PRAGMA table_info` /
  `index_list` for a subsystem that has already been verified in this process.
- **The per-connection memory budget must be coherent with the ceiling.** `cache_size=-64000` and
  `mmap_size=256MB` are reasonable for a handful of reused connections and incoherent at 43.
- **A loud failure, not silent growth,** when the ceiling is hit — per the repo rule that a control which
  is not real at the moment it matters is worse than no control.

---

## Part C — the consumer query surface

The structural problem: **a consumer has nowhere to ask spendguard a question, so it ends up with the
ledger open in-process.** `config.db_path()` is one file opened by ~8 subsystems, and any process importing
the gate inherits that posture. honestreview is simply the first consumer big enough to make it visible.

Ground first: `receipt.py` and `report.py` exist, as do the `spendguard_*` MCP tools. **Find what already
answers these questions and extend it. Prove a capability is missing before building it.**

Design a read surface a consumer can use **without opening the database**, exposed as
**library · CLI · MCP** — one implementation, three thin skins, not three implementations.

- **The consumer never opens `spend.db` or `lane_queue.db`.** That is the point.
- **One owner of the connection and the pragma posture.** Consumers cannot inherit or override it.
- **Bounded by construction** — every query carries a row cap and a time budget. A query that would exceed
  its budget fails loud and says so. No caller can request an unbounded cursor walk.
- **Never `SELECT *` on `calls` / `spend_events`** — named columns only, so no caller accidentally pulls
  ~6 KB/row of payload.
- **Typed answers, not rows** — "what did intent X cost today", "is the plan on overage", "what did this
  conversation cost", "recent calls for repo R" — so callers don't re-implement aggregation.
- Honour the house cost-display rule: real-$ split into API / Subscription / Remote, est-value as a separate
  axis after `::`, **never summed**.

Then migrate spendguard's own in-repo direct openers to the surface, leaving the raw path to internals only.

---

## Part D — ledger hygiene (gated on a verified backup)

- **Back up first, and verify the backup before touching the original.** `~/.spendguard/spend.db` is 4.4 GB
  with a live 64 MB WAL. House rule: never copy a live `db` + `wal`. The B2 ledger job runs 03:45 —
  sequence against it, and use the backup API or a checkpointed copy.
- `wal_checkpoint(TRUNCATE)` then `VACUUM`. Expect to reclaim **~3.5 GB**. Measure the file before and
  after, **and measure whether it changes the Part A profile at all — report that number even if it turns
  out to be zero.** Nobody currently knows that VACUUM will help the CPU, and an assumed win is worse than
  a measured null.
- The `tune_ledger_connection` docstring says "a periodic `wal_checkpoint(TRUNCATE)` drains it".
  **Determine whether that periodic drain actually runs.** If it doesn't, that is a finding in itself.
- Decide whether `auto_vacuum=INCREMENTAL` is right going forward, and whether `~/.spendguard` should be
  excluded from Spotlight — `mds_stores` was observed at 90% CPU alongside this churn, and a 4.4 GB db with
  a constantly-rewritten 64 MB WAL is exactly what makes the indexer loop.

---

## Guards (§anti-amnesia — prose does not count)

Nothing here is done until it cannot regress:

1. A test that **fails if any consumer outside spendguard's internals opens `config.db_path()` directly.**
2. A test that **fails if live connections per process exceed the Part B ceiling.**
3. A test that **fails if a ledger query is issued without a row cap and a time budget.**
4. A test asserting **no `SELECT *` against `calls` / `spend_events`** in the query surface.
5. A regression guard for whatever Part A actually finds — a bounded assertion on the real mechanism
   (statements per recorded call, schema-ensures per process), not a comment.
6. A bloat check reporting free-page ratio, so 3.5 GB of dead pages becomes visible instead of being
   discovered by a CPU investigation months later.

## Verify green

Run every command and paste **full** output, no piping or truncation:

- the repo's offline suite
- `ruff` (the suite's lint gate)
- `spendguard doctor` — must print `ENFORCING HERE: YES`
- the Part A reproduction before and after, with the profile and the setup-vs-query cost split
- `lsof -p <pid> | grep -c '\.spendguard/.*\.db$'` before and after Part B — the 43 must come down
- `ls -l ~/.spendguard/spend.db ~/.spendguard/spend.db-wal` before and after Part D

If a sandbox denies `~/.spendguard` writes, a local port bind, or `sudo` for `py-spy`, **say so verbatim** —
the host re-runs it.

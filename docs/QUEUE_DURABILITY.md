# Durable lane queue

The lane queue accepts work while lanes are saturated and drains it later through the existing governed execution path. It adds persistence, priority, leasing, crash recovery, and bounded retention; it does not replace dispatch admission, lane selection, or provider settlement ([`src/spendguard/lane_queue.py:1`](https://github.com/llmspendguard/llm-spendguard/blob/main/src/spendguard/lane_queue.py#L1), [`src/spendguard/lane_queue.py:1045`](https://github.com/llmspendguard/llm-spendguard/blob/main/src/spendguard/lane_queue.py#L1045)).

## State isolation and migration

Queue state lives in `lane_queue.db`, not the `spend.db` money ledger. The default is `$SPENDGUARD_HOME/lane_queue.db`, overrideable with `budget.lane_queue_db_path`. This separation keeps large task/result rows, purge scans, snapshots, and queue WAL traffic from contending with or inflating money-of-record storage ([`src/spendguard/config.py:764`](https://github.com/llmspendguard/llm-spendguard/blob/main/src/spendguard/config.py#L764), [`src/spendguard/lane_queue.py:165`](https://github.com/llmspendguard/llm-spendguard/blob/main/src/spendguard/lane_queue.py#L165)).

`migrate_from_ledger()` is the one-time, idempotent migration. With the drain stopped and long-lived writers updated, it copies only non-terminal `pending` and `leased` work into the queue database, assigns fresh row IDs, then drops the old queue table from `spend.db`. Terminal history is intentionally not copied ([`src/spendguard/lane_queue.py:190`](https://github.com/llmspendguard/llm-spendguard/blob/main/src/spendguard/lane_queue.py#L190)). The CLI exposes it as `spendguard lanes --migrate-queue-db` ([`src/spendguard/lanes.py:735`](https://github.com/llmspendguard/llm-spendguard/blob/main/src/spendguard/lanes.py#L735)).

Connections use the shared SQLite tuning: WAL mode, bounded busy wait, `synchronous=NORMAL`, a WAL journal-size limit, and pooled per-thread connections ([`src/spendguard/config.py:822`](https://github.com/llmspendguard/llm-spendguard/blob/main/src/spendguard/config.py#L822)).

## Content identity and deduplication

The canonical identity is a SHA-256 digest over the result-determining tuple `(intent, task, system, reasoning)`. Each UTF-8 field is length-prefixed before hashing to prevent delimiter collisions. Model and lane are excluded because they are selected at drain time; the identity means “answer this exact work,” not “run it on this exact executor” ([`src/spendguard/lane_queue.py:147`](https://github.com/llmspendguard/llm-spendguard/blob/main/src/spendguard/lane_queue.py#L147)).

Deduplication is layered:

- **Enqueue:** `enqueue_many(..., dedup=True)` reuses a matching non-failed row, preferring a completed result over an in-flight row. Duplicates within the same enqueue transaction also share an ID. `dedup=False` permits intentional independent repeats, but still stores the fingerprint for downstream handling ([`src/spendguard/lane_queue.py:253`](https://github.com/llmspendguard/llm-spendguard/blob/main/src/spendguard/lane_queue.py#L253)).
- **Drain preflight:** `dedup_pending_against_done()` backfills fingerprints for rows produced by older enqueuers, in bounded chunks, then settles pending duplicates from a completed result without an API call ([`src/spendguard/lane_queue.py:307`](https://github.com/llmspendguard/llm-spendguard/blob/main/src/spendguard/lane_queue.py#L307), [`src/spendguard/lane_queue.py:1098`](https://github.com/llmspendguard/llm-spendguard/blob/main/src/spendguard/lane_queue.py#L1098)).
- **Lease:** a drain batch leases at most one row for each non-null fingerprint. Same-fingerprint twins remain pending until the selected row settles ([`src/spendguard/lane_queue.py:357`](https://github.com/llmspendguard/llm-spendguard/blob/main/src/spendguard/lane_queue.py#L357)).
- **Settlement:** a successful row atomically marks other pending rows with the same fingerprint `done`, copies the bounded result, records `lane='content-addressed-dedup'`, and sets `billed=0` ([`src/spendguard/lane_queue.py:425`](https://github.com/llmspendguard/llm-spendguard/blob/main/src/spendguard/lane_queue.py#L425)).

Failed rows are not reusable dedup targets: newly enqueued identical work receives a fresh row. Null fingerprints are treated as distinct at lease time rather than guessed equal.

## Leasing, recovery, and settlement

`lease()` begins an immediate transaction before selection, reclaims expired leases, and atomically changes selected rows to `leased` with a worker, expiration, and incremented attempt count. Expired work returns to `pending` while attempts remain; exhausted work becomes `failed` ([`src/spendguard/lane_queue.py:357`](https://github.com/llmspendguard/llm-spendguard/blob/main/src/spendguard/lane_queue.py#L357)). The total ordering favors priority, realtime service class, earliest SLA, then oldest ID.

`settle()` is state-guarded so only a currently `leased` or `queued_batch` row can be settled. Successful work becomes `done`; transient failures are deferred and retried within the attempt budget; deterministic failures fail fast. Capacity saturation parks work without consuming a failure attempt. Batch-offloaded work remains separately collectible so it is not also retried in realtime and double-billed ([`src/spendguard/lane_queue.py:425`](https://github.com/llmspendguard/llm-spendguard/blob/main/src/spendguard/lane_queue.py#L425)).

## Bounded housekeeping

Terminal rows are retained for a configurable period, then `purge()` archives and deletes them in bounded chunks. Each short transaction appends the selected rows before deleting them; a crash can duplicate archive entries but cannot delete an unarchived chunk. Pending and leased work are never purged ([`src/spendguard/lane_queue.py:947`](https://github.com/llmspendguard/llm-spendguard/blob/main/src/spendguard/lane_queue.py#L947)).

Housekeeping is bounded in three dimensions:

- purge frequency is throttled by `queue_purge_min_interval_s` rather than running every drain cycle ([`src/spendguard/lane_queue.py:937`](https://github.com/llmspendguard/llm-spendguard/blob/main/src/spendguard/lane_queue.py#L937));
- purge transaction size is capped by `queue_purge_chunk` ([`src/spendguard/lane_queue.py:947`](https://github.com/llmspendguard/llm-spendguard/blob/main/src/spendguard/lane_queue.py#L947));
- `lane_queue_archive.jsonl` rotates to one `.1` generation after `queue_archive_max_mb` ([`src/spendguard/lane_queue.py:925`](https://github.com/llmspendguard/llm-spendguard/blob/main/src/spendguard/lane_queue.py#L925)).

## Single-instance drain

`drain()` acquires a non-blocking process lock before entering the loop. If another drainer owns the lock, it returns a skipped summary instead of starting a competing executor; the file descriptor is released in `finally`, including on deliberate-stop exceptions ([`src/spendguard/lane_queue.py:1045`](https://github.com/llmspendguard/llm-spendguard/blob/main/src/spendguard/lane_queue.py#L1045)). SQLite leasing remains transactional, but the single drainer also prevents duplicate planning, provider polling, and competing drain loops.

The loop performs preflight dedup, leases bounded batches, runs them through `lane_balance.bulk_delegate`, and settles each result. Foreground mode stops after configured empty rounds; daemon mode waits for new work. A `max_iters` stop leaves durable rows for the next run and reports remaining work rather than claiming completion ([`src/spendguard/lane_queue.py:1066`](https://github.com/llmspendguard/llm-spendguard/blob/main/src/spendguard/lane_queue.py#L1066)).

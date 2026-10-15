# Exactly-once and never-lose-work chaos extension

## Part 0 — grounding before implementation

The initial read and file:line findings were reported in the implementation session before edits.
The references below point to the resulting source (line numbers moved as safeguards were added).

| Guarantee | Existing infrastructure | Unguarded edge and classification |
| --- | --- | --- |
| Exactly-once | `submit.py:445` / `:576` build and gate chat/message batches; `callio.py:646` / `:707` collect by custom ID. `batch_tracker.py:118,130,348` derives a row-set key, reconciles OpenAI metadata, and adopts before creating. `:171,195,217,249,321` handles Anthropic pending-before-create, confirm, orphan scan and in-flight hold. `lane_queue.py:561,598` durably marks and collects; `:505` guards settlement writes. | Custom IDs identify requests; they are not provider idempotency tokens. Retry safety belongs to `submit_offload` and the durable queue row set. String ordinals previously restarted within shards: **MIS-WIRING**, fixed by assigning IDs before the fan. A tracker could conclude “settled” from an empty outstanding-row lookup without proving submitted rows still had outcomes: **ABSENT evidence/assertion**, fixed with a manifest on the existing `batch_jobs` record. |
| Never-lose-work | `whole_job.py:162,258,280` returns pending handles, fsyncs the pending file, and recovers it. Persist failures and eligible batch failures are already named in the receipt. `lane_queue.py:357,430,935,1100` owns lease recovery, guarded settlement, queue depth and drain. `:894` owns deadline-limited synchronous holds. | Whole-job execution could return fewer results without naming the missing units: **ABSENT final accounting assertion**, fixed with per-ID named gaps and a recorded partition proof. A failed hold cleanup could leave an expired lease reclaimable: **MIS-WIRING**, fixed by checking terminal persistence and refusing expired durable deadlines during subsequent leasing. |

The raw submit helpers remain explicit create primitives: callers that want crash/retry
deduplication must own durable row identity and use the existing `submit_offload` path.
Two unrelated calls both using the auto-ID `task-0` are not the same logical job.
`whole_job` owns its pending handle and reports failed persistence; it does not promise that
independent invocations without a shared job identity are deduplicated.

## Implementation

- `work_integrity.WorkIntegrityViolation` is a typed deliberate stop derived from the existing gate refusal.
- `work_integrity.WorkIntegrityProof.assert_work_partition` logs commit or bug evidence via `gate._log`;
  duplicate, missing, or extra dispositions fail closed.
- `submit._assign_batch_ordinals` preserves whole-set ordinals through sharding and refuses duplicate custom IDs
  before submission. Whole-job IDs are also checked before execution.
- `batch_tracker.assert_batch_terminal_manifest` checks the existing job's submitted-ID manifest against durable
  queue outcomes before `poll` commits `settled`. Missing/corrupt legacy evidence refuses that commit.
  Manifests and job registration commit together; adoption preserves the manifest.
- `whole_job.run_jobs` names missing executor results and includes proof events in its receipt.
  `collect_jobs` names missing terminal batch results when the handle supplies job IDs.
- `lane_queue.collect_batched` checks disjoint outcomes and verifies persisted successes before counting them.
  Free holds check terminal persistence; expired deadlines become named failed queue outcomes before re-leasing.

No new scheduler, queue, transport, retry system, or paid-job persistence mechanism was introduced.
No prices, budgets, recovery thresholds, or deadlines were added. No in-flight work is cancelled.
The existing scoped-budget test fixture now declares the two jobs it returns instead of declaring an empty set.

## Contracts and absence gates

`tests/chaos/exactly_once_faults.py` reuses the existing fake SDKs with real submit builders, gated submit,
provider reconciliation, pending storage, job registration, durable queue marking and collectors. It injects
OpenAI submit-to-mark and Anthropic create-to-confirm crashes, then exercises adoption, duplicate submission,
concurrent collection, collection replay, whole-set shard ordinals, duplicate-ID refusal and a deleted terminal row.

`tests/chaos/never_lose_work_faults.py` uses real whole-job submit/persist with fake provider transport, a real
failed durable-file write, a short executor response, a crash after one drain result committed, lease recovery,
concurrent expired holds, and a failed hold cleanup. Every unit is completed or named; expired holds cannot re-lease.
Both scripts call `spendguard.require()` and make no live provider requests.

`tests/chaos/mutation_proof.py` prints lane, exactly-once and never-lose-work matrices. All intact contracts must
be GREEN and every disabled production rung must be RED. `test_chaos_runner.py` includes them in default CI.

`tests/test_work_integrity_axis4.py` inventories 24 ownership-transfer edges plus 10 durable state writes across
package and script sources, reports UNREVIEWED separately from INSUFFICIENT, rejects added/removed edges, and
checks production commit assertion placement. Synthetic new submit/state-write edges and unparseable input are
negative controls. This is a reviewed static absence gate plus executable chaos evidence, not a live-provider
fidelity or distributed exactly-once theorem.

## New symbols

Production: `WorkIntegrityViolation`, `WorkIntegrityProof`, `assert_work_partition`,
`_assign_batch_ordinals`, `assert_batch_terminal_manifest`.

Contracts/gates: `run_exactly_once_chaos_contract`, `run_message_exactly_once_chaos_contract`,
`run_never_lose_work_chaos_contract`, `work_mutation_matrix`,
`test_work_integrity_mutation_contracts`, `inventory_work_ownership_edges`.

## Verification results

All tests used `.venv.nosync/bin/python`.

| Check | Result |
| --- | --- |
| `-m pytest -n auto -q` | 547/547 passed; final process exit 0 |
| `tests/chaos/exactly_once_faults.py` | GREEN, both providers |
| `tests/chaos/never_lose_work_faults.py` | GREEN |
| `tests/chaos/mutation_proof.py` | All three intact GREEN; all 13 broken rungs RED |
| `tests/test_lane_recovery_axis4.py` | 22/22 routing edges, 15/15 SDK sinks; zero unreviewed/insufficient |
| `tests/test_work_integrity_axis4.py` | 24/24 ownership edges, 10/10 state writes; zero unreviewed/insufficient |
| `tests/test_names_stay_unique.py` | 63 colliding names, all judged; no new collision |
| `.venv.nosync/bin/ruff check src tests` | All checks passed |
| `git diff --check` | Passed |

```text
lane                 intact                              GREEN
lane                 disable_retry                       RED
lane                 disable_shed                        RED
lane                 disable_queue                       RED
lane                 allow_ungated_metered               RED
exactly-once         intact                              GREEN
exactly-once         disable_idempotency                 RED
exactly-once         drop_persisted_handle               RED
exactly-once         disable_message_idempotency         RED
exactly-once         drop_message_pending_record         RED
exactly-once         disable_terminal_manifest_assertion RED
exactly-once         disable_global_ordinals             RED
never-lose-work      intact                              GREEN
never-lose-work      swallow_persist_failure             RED
never-lose-work      rebuy_expired_hold                  RED
never-lose-work      disable_disposition_assertion       RED
```

Files changed for this task:

```text
src/spendguard/work_integrity.py
src/spendguard/submit.py
src/spendguard/batch_tracker.py
src/spendguard/lane_queue.py
src/spendguard/whole_job.py
tests/chaos/exactly_once_faults.py
tests/chaos/never_lose_work_faults.py
tests/chaos/mutation_proof.py
tests/chaos/test_chaos_runner.py
tests/test_work_integrity_axis4.py
tests/test_scoped_budget_ceiling.py
docs/CHAOS_WORK_INTEGRITY.md
```

The first suite run exposed stale editable-install metadata and an inconsistent budget-test fixture;
the environment was refreshed with `pip install -e . --no-build-isolation`, and the fixture was corrected.
A subsequent in-progress run caught the new SQL inventory missing a correlated-subquery update;
the inventory was fixed and the final complete suite passed.
Doctor confirmed `ENFORCING HERE: YES`; its initial overall refusal also reported pre-existing ledger issues.
No live LLM requests were sent by these contracts. No git commit was created.

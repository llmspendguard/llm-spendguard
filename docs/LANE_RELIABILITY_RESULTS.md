# Lane reliability implementation and verification

No commit was made. Pre-existing untracked files were left untouched.

## Part 0

See [the pre-edit grounding](LANE_RELIABILITY_GROUNDING.md) for original file:line evidence and the
hook/panel boundary. Retry, shedding, queue and gate infrastructure existed; the failures were wiring
gaps. The concurrent chaos tier, mutation harness and explicit recovery-proof contract were absent.
No `resilient_dispatch_queue` implementation exists in this checkout; `lane_queue` owns durable retries.
Honestreview's external hook implementation is outside this repository.

## Behavior

`adapters._call_once` confirms misses with configured retries, reuses quality-equivalent shedding and
the ready-lane availability router, holds recovery in the existing durable queue, rechecks alternatives
before paid admission, and explicitly invokes the normal spend door with fail-closed enforcement.
Pinned votes preserve their vendor. Cooldowns no longer silently authorize the paid API. Expired queue
holds settle terminally; they cannot be re-bought by a later drain. Recovery never cancels an invocation.
Outcome facts are returned and written through the existing gate audit/telemetry pipeline.

## Changed files

Modified:

* `pyproject.toml`
* `src/spendguard/adapters.py`
* `src/spendguard/config_schema.py`
* `src/spendguard/lane_queue.py`
* `src/spendguard/vendor_call.py`
* `tests/conftest.py`
* `tests/test_runner.py`
* `tests/test_ensure_success_routing.py`
* `tests/test_executor_pool.py`
* `tests/test_lane_metered_fallback_separation.py`
* `tests/test_lane_shed_to_metered.py`
* `tests/test_lane_transient_retry.py`
* `tests/test_universal_admission.py`

Added:

* `src/spendguard/lane_recovery.py`
* `tests/test_lane_recovery_axis4.py`
* `tests/chaos/lane_faults.py`
* `tests/chaos/mutation_proof.py`
* `tests/chaos/test_chaos_runner.py`
* `tests/chaos/live_fidelity.py`
* `tests/chaos/README.md`
* `docs/LANE_RELIABILITY_GROUNDING.md`
* `docs/LANE_RELIABILITY_RESULTS.md`

## New symbols

Production:

* `LaneRecoveryViolation`
* `LaneRecoveryProof`
* `LaneRecoveryProof.recovery_event`
* `LaneRecoveryProof.assert_free_exhausted`
* `LaneRecoveryProof.authorize_metered_recovery`
* `LaneRecoveryProof.assert_metered_recovery`
* `recovery_setting`
* `lane_failure_shape`
* `retry_free_lane`
* `lane_queue.hold_free_recovery`
* Nested adapter callbacks `_shed_free_recovery` and `_poll_free_recovery`

Verification entry points: `RecoveringFaultLane`, `run_lane_chaos_contract`, `lane_mutation_matrix`,
`test_lane_ladder_mutation_contract`, `inventory_lane_paid_edges`, `live_lane_fidelity_main`.

## Results

All tests used `.venv.nosync/bin/python`.

| Check | Result |
| --- | --- |
| `-m pytest -n auto -q --junitxml=/private/tmp/lane-reliability-final.xml` | 545 passed, 0 failures, 0 errors, 0 skipped; 133.219 seconds |
| `-m pytest -m chaos -q -s` | passed; matrix below |
| `tests/chaos/mutation_proof.py` | all five expected outcomes verified |
| `tests/test_lane_recovery_axis4.py` | 22/22 routing edges + 15/15 SDK sinks; 254 Python files; UNREVIEWED=0, INSUFFICIENT=0 |
| `tests/test_names_stay_unique.py` | passed; 63 existing collisions all judged, 0 unjudged |
| `-m ruff check src tests` | all checks passed |
| `git diff --check` | passed |
| interpreter doctor | ENFORCING HERE: YES |

| Mutation | Chaos result | Required outcome |
| --- | --- | --- |
| intact | GREEN | pass |
| disable_retry | RED | fail |
| disable_shed | RED | fail |
| disable_queue | RED | fail |
| allow_ungated_metered | RED | fail |

The default runs use simulated transport and real local queue/audit records. They submit no paid requests.
The opt-in live fidelity runner was implemented but not run; it requires a separate estimate plus an
explicit approved budget. Static coverage is an inventory of reviewed routing and SDK boundaries,
not a claim to resolve arbitrary dynamic Python or external hook code. See the chaos README for commands.

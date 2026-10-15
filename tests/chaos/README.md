# Lane reliability chaos gate

All commands use the repository interpreter. Default runs use fake transport and spend no API money.
They exercise the real lane registry, adapters fallback ladder, durable queue and normal spend door.

```
.venv.nosync/bin/python -m pytest -m chaos -q -s
.venv.nosync/bin/python tests/chaos/mutation_proof.py
.venv.nosync/bin/python tests/test_lane_recovery_axis4.py
```

The chaos runner is collected by ordinary `pytest` in CI, including its mutation proof. It is not
an optional excluded marker. The subprocess mutations replace real rung functions/selection responses;
the unchanged concurrent behavioral contract must pass intact and fail for every broken rung.

Fixed HTTP/transport/logout envelopes classify as transient. Unknown prose remains ambiguous and is
confirmed by repeated observations, the existing structural auth probe, and the existing fallback
outcome learning. No new semantic keyword classifier or paid judge is introduced. Vendor pinning is
preserved: another vendor is not an eligible substitute for a pinned panel vote.

`lane_recovery` configuration (all read at call time; environment wins):

| Key | Environment | Default |
| --- | --- | --- |
| attempts | SPENDGUARD_LANE_ATTEMPTS | 2 total attempts |
| backoff_s | SPENDGUARD_LANE_BACKOFF_S | 1.2 seconds |
| backoff_multiplier | SPENDGUARD_LANE_BACKOFF_MULTIPLIER | 2 |
| queue_deadline_s | SPENDGUARD_LANE_QUEUE_DEADLINE_S | 15 seconds |
| queue_poll_s | SPENDGUARD_LANE_QUEUE_POLL_S | 1 second |
| chaos_callers | SPENDGUARD_LANE_CHAOS_CALLERS | 2 live callers |

The offline script runner uses zero-duration retry/hold settings; the separate chaos tier explicitly
sets positive delays and verifies backoff, concurrency and recovery. A hold's deadline bounds new
attempt admission. Running calls finish; they are never cancelled by this ladder. Expired holds are
settled terminally so a later queue drain cannot buy the same work after paid fallback.

Recovery facts and rung outcomes are returned as `lane_recovery` and recorded through the existing
gate audit/telemetry. Missing retry/shed/queue evidence or missing gate authorization raises
`LaneRecoveryViolation`, a deliberate `SpendGateRefused` subtype.

The Axis-4 gate inventories package and script routing edges plus SDK sinks. Unreviewed signatures,
new sinks, unparseable files, and removed/duplicated reviewed edges fail. Its denominator is explicit;
it does not claim static resolution of arbitrary dynamic Python. Concurrent execution and mutations
cover the shared runtime boundary. Honestreview's external hook source is outside this checkout.

Optional real-lane fidelity requires two separate commands and an explicit approved budget:

```
.venv.nosync/bin/python tests/chaos/live_fidelity.py --model openai:gpt-5.5 --estimate
SPENDGUARD_CHAOS_LIVE_BUDGET_USD=<approved-budget> .venv.nosync/bin/python tests/chaos/live_fidelity.py --model openai:gpt-5.5 --live
```

The estimate records the call count separately from estimated subscription value, using canonical
pricing. The live probe injects the first transport failure then invokes the real executor under
concurrency. Even when opted in, it forbids metered fallback; its budget is an approval ceiling,
not permission to silently substitute paid work. No live run was needed for the default CI proof.

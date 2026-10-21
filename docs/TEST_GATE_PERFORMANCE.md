# Test gate performance and local iteration

The merge gate still runs every hosted case as an isolated subprocess, including
all four chaos cases, with branch coverage on Python 3.11 and 3.12. The calibrated
per-file budget and its controller-measured runner scale are unchanged. Speedups
come from caching the installed environment and distributing complete cases
across jobs.

## Run the gate

Use a gated repo virtualenv and point `PYTHONPATH` at the current worktree's `src`
(editable installs can point at another checkout). Confirm `spendguard doctor`
reports `ENFORCING HERE: YES`. Collect through the runner, never individual script
files directly:

```sh
export PYTHONPATH="$PWD/src"
SPENDGUARD_COVERAGE=1 python -m pytest -n auto --junitxml=full-gate.xml
coverage combine --keep
coverage report --fail-under=40
coverage report --fail-under=78 --include="*/gate.py,*/guard.py,*/budget.py,*/reconcile.py,*/ledger_sync.py,*/pricing.py,*/attribution.py,*/adapters.py,*/tag.py,*/backfill.py,*/deid.py,*/litellm_adapter.py,*/bedrock_adapter.py"
```

Start with an empty, dedicated `COVERAGE_FILE` directory for measurement so older
coverage cannot inflate the result. In CI, artifacts are separated by Python
version and shard. The aggregation job verifies all shard reports and coverage
files exist, with every expected case passed exactly once, before combining
coverage and enforcing both floors. Missing, duplicate, stale or failed evidence
raises `GateShardEvidenceError`; it never becomes a smaller passing gate.

## Stored timing and sharding

`.test_durations.json` is the single configuration for shard count and measured
case durations. Its `shard_count` is four. The planner orders complete cases by
descending measured duration and assigns each to the least-loaded shard, using
case count and shard index to break ties deterministically. Collection must match
the entire stored inventory before any shard selection; adding/removing a case
requires refreshing the manifest from a successful complete gate report:

```sh
python tests/_gate_shard_plan.py refresh --junit full-gate.xml --shard-count 4 --output .test_durations.json
python tests/_gate_shard_plan.py matrix --timings .test_durations.json
```

For a local shard, set `SPENDGUARD_GATE_SHARD_INDEX` to an index emitted by
`matrix`. `SPENDGUARD_GATE_TIMINGS` can select another complete timing manifest.
Do not apply coverage floors to individual shards. Long cases remain indivisible:
balancing total work cannot promise total wall-clock divided by shard count when
one case dominates a shard.

## Local iteration tiers and deferred work

T2 is the complete gate above, retained on both Python versions. Until the
in-process migration below is complete, use the existing isolated focused loop:

```sh
python -m pytest tests/test_runner.py -k 'ci_environment_cache or gate_shard_integrity' -x
```

The proposed T0 `test-fast` (changed-related cases in process, no coverage, fail
fast) and T1 `test-smoke` (all non-heavy cases in process with xdist, no coverage)
are **deferred**, not substitutes for T2. Current scripts use `os.execv`,
`sys.exit`, `atexit`, captured ledger handles and shared-module monkeypatches
without a common teardown contract. Running them in one process can terminate
the run or contaminate later cases. They need fixture conversion and explicit
restoration guards before these targets can honestly be added. No heavy marker
or arbitrary duration cutoff is claimed while those tiers are unavailable.

Python 3.12 sysmon is also deferred: coverage 7.16.2 falls back to the C tracer
when branch coverage is enabled on 3.12. `.coveragerc` retains branch coverage;
setting `COVERAGE_CORE=sysmon` would only advertise an unavailable optimization.

Lazy SDK loading is deferred as well. The experiment removed eager imports, but
required patch failures changed from installation warnings to refused imports.
Keeping that fail-closed behavior needs approval because the requested refactor
must preserve error behavior. The SDK gate implementation remains unchanged.

## Measurements

On the same local Python 3.11 environment, the baseline complete gate passed
643 cases in 485.58 seconds. The deferred lazy SDK experiment passed 644 cases (one new
guard) in 200.29 seconds, with fresh coverage at 70% package and 81% core. After
the environment guard it passed 645 in 148.38 seconds. The extra difference
between post-fix runs is run-to-run variation, not an attributed install benefit.
In that experiment, importtime fell from 2.166868 to 0.185913 seconds; neither
`anthropic` nor `openai` was loaded by `import spendguard`. Those import savings
are not part of this change. Final cold scratch installs on Python 3.12 with
identical current extras, the Pydantic floor, the coverage pin and PyYAML took
22.83 seconds with pip and 4.89 seconds with uv (approximately 4.7 times faster).
Both used fresh virtualenvs and disabled installer caches; the complete logs are
`testspeed-pip-finalguard-install.log` and `testspeed-uv-finalguard-install.log`.
Hosted cache restoration and hosted wall-clock require the actual CI run; local
timings are not hosted measurements.

The final retained candidate, with eager SDK loading restored, passed all 645
hosted cases in 452.88 seconds locally. Fresh coverage was 70% package and 81%
core, passing the unchanged 40% and 78% floors. The difference from the baseline
unsharded run is not evidence of an optimization; run-to-run variation remains.
The retained Python 3.12 candidate also passed all 645 cases in 334.78 seconds
locally, with fresh 70% package and 81% core coverage using default CTracer and
branch coverage. Both coverage floors passed on both Python versions.

The committed four-shard manifest assigns 158, 162, 163 and 162 cases, with
measured total case work of 1488.920, 1490.310, 1489.142 and 1489.106 seconds
respectively. These totals include cases executed concurrently within each job;
they are not shard wall-clock measurements. The slowest indivisible case took
191.711 seconds locally. Applying the requested hosted projection factor of
approximately two gives a lower bound of approximately 383.4 seconds for the
hosted gate before installation and aggregation overhead. Actual parallel shard
wall-clock remains pending the hosted CI run.

All four local shards passed: 158, 162, 163 and 162 cases in 235.79, 156.05,
191.86 and 103.77 seconds respectively. Their exact union contained all 645
cases, including all four chaos cases. The first two shard measurements shared
the machine with the Python 3.12 verification, so these timings include that
contention. Artifact verification passed complete reports, SHA-256 inventories
and readable branch coverage. Combining 651 coverage files identified 42
duplicate inputs; original files were retained with `--keep`. Combined coverage
remained 70% package and 81% core, passing both floors.

Using the requested approximate hosted factor of two, the measured slowest
shard projects to 471.6 seconds versus the baseline's 971.2 seconds, about a 51%
wall-clock reduction before installation and aggregation overhead. This compares
local measurements under different contention conditions; hosted parallel
wall-clock and cache restoration still require the actual CI run.

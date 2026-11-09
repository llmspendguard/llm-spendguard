# Readiness

Definition revision: 7

Square means every checklist item has a reviewed deterministic gate and every gate passes.
`spendguard selfcheck` runs each gate and exits nonzero for FAIL or UNREVIEWED.
Coverage counts PASS and FAIL items with reviewed gates; UNREVIEWED counts as neither coverage nor success.
A supplied clean-environment report must match the current input fingerprint and its complete verification logs.

## Project invariants

- [ ] **wheel-source-module-conformance** — A clean offline wheel contains exactly every recursive source module and no stale modules.
  Gate: `spendguard.capability_readiness_gates:assess_wheel_source_conformance_readiness`; inputs: `{"scripts": ["tests/test_wheel_ships_every_source_subpackage.py"]}`.

- [ ] **surface-parity** — Every capability is exposed on CLI, API and MCP except declared deliberate absences.
  Gate: `spendguard.capability_readiness_gates:run_capability_script_readiness_gate`; inputs: `{"scripts": ["tests/test_capability_surface_parity_guard.py"]}`.

- [ ] **lane-leak-tripwire** — No protected subscription lane or metered provider was billed inside the lookback window.
  Gate: `spendguard.capability_readiness_gates:assess_protected_lane_leak_readiness`; inputs: `{"hours": 1.0}`.

- [ ] **lane-queue-maintenance** — Lane queue size, free-page ratio, and expired terminal retention remain within configured limits.
  Gate: `spendguard.capability_readiness_gates:assess_lane_queue_maintenance_readiness`; inputs: `{}`.

- [ ] **help-non-trivial** — Every CLI command exposes real flags or its explicit no-flags declaration.
  Gate: `spendguard.capability_readiness_gates:run_capability_script_readiness_gate`; inputs: `{"scripts": ["tests/test_capability_cli_help_guard.py"]}`.

- [ ] **docs-present** — Generated API reference, parameter contracts, and handwritten source references match the current tree.
  Gate: `spendguard.capability_readiness_gates:run_capability_script_readiness_gate`; inputs: `{"scripts": ["tests/test_capability_documentation_guard.py", "tests/test_docs_reference_live_source_paths.py"]}`.

- [ ] **no-release-on-red-ci** — Release promotion fails closed when CI is red or unknown.
  Gate: `spendguard.capability_readiness_gates:run_capability_script_readiness_gate`; inputs: `{"scripts": ["tests/test_release_green_pointer.py"]}`.

- [ ] **env-parity-in-verification** — Current source passes required checks in independent clean environments.
  Gate: `spendguard.capability_readiness_gates:assess_capability_environment_evidence`; inputs: `{"patterns": ["src/spendguard/**/*.py", "src/spendguard/capability*.json", "src/spendguard/interface/capability*.jsonl", "src/spendguard/architectural_fitness_baseline.json", "scripts/test/*.py", "tests/**/*.py", "pyproject.toml", ".github/workflows/*", "docs/*.md", "LICENSE", "SECURITY.md", "CONTRIBUTING.md", "README.md", "CHANGELOG.md", "src/spendguard/platform/distribution_build_wheels/*.whl"], "required_checks": ["ruff", "name-uniqueness", "surface-suites", "offline-audit"]}`.

- [ ] **money-correctness** — Money scale, exact ledger arithmetic and global receipt allocation remain correct.
  Gate: `spendguard.capability_readiness_gates:run_capability_script_readiness_gate`; inputs: `{"scripts": ["tests/test_money_scale.py", "tests/test_ledger_money_is_exact_decimal.py", "tests/test_receipt_global_dollar_allocation.py"]}`.

- [ ] **effort-learn-loop** — Reasoning-effort is on the measured frontier, titrated, best-value-selected and applied per panel member without collapse.
  Gate: `spendguard.capability_readiness_gates:run_capability_script_readiness_gate`; inputs: `{"scripts": ["tests/test_effort_on_frontier.py", "tests/test_best_value_select.py", "tests/test_bakeoff_effort_sweep.py", "tests/test_effort_titration.py", "tests/test_lane_records_applied_effort.py", "tests/test_panel_effort_autoapply.py"]}`.

- [ ] **budget-projection-snapshot-seam** — Grounded monetary read roots use the snapshot seam; projections require finite contracts; mutation controls reject raw writer reads.
  Gate: `spendguard.capability_readiness_gates:run_capability_script_readiness_gate`; inputs: `{"scripts": ["tests/test_budget_projection_seam_guard.py"]}`.

- [ ] **spend-daily-rollup-reconciles-to-heap** — Closed reporting-day aggregates preserve exact heap money, precise dirty-day invalidation with compare-and-clear, off-hot-path snapshot audits, compact grain and snapshot semantics.
  Gate: `spendguard.capability_readiness_gates:run_capability_script_readiness_gate`; inputs: `{"scripts": ["tests/test_ledger_daily_rollup.py"]}`.

- [ ] **ledger-read-never-spurious-refuse** — Cap and delegate admission reads serve committed snapshots despite writer transactions or expired request scopes; genuine query deadlines still refuse.
  Gate: `spendguard.capability_readiness_gates:assess_ledger_read_never_spurious_refuse_readiness`; inputs: `{"scripts": ["tests/test_ledger_read_never_spurious_refuse.py"]}`.

- [ ] **ledger-write-never-refused-on-expired-ambient-scope** — Serialized durable writes persist delegate and dead-letter evidence despite expired ambient request time; genuine abandonment and operation deadlines still abort.
  Gate: `spendguard.capability_readiness_gates:assess_ledger_write_ambient_scope_persistence_readiness`; inputs: `{"scripts": ["tests/test_ledger_write_never_refused_on_expired_ambient_scope.py"]}`.

- [ ] **ambient-scope-never-refuses-ledger-ops** — Reads, serialized writes, connection admission, content retention and dead-letter drains survive elapsed ambient request time; cancellation and operation budgets still refuse.
  Gate: `spendguard.capability_readiness_gates:assess_ambient_scope_ledger_operation_readiness`; inputs: `{"scripts": ["tests/test_ambient_scope_never_refuses_ledger_ops.py"]}`.

## External standards — reviewed offline subsets

PASS covers the declared offline subset, not certification or an external badge. Reviewed N/A checks remain tracked below.

- [ ] [OpenSSF Scorecard mapping.](https://scorecard.dev/) — Reviewed offline subset only; no external certification claimed.
  Gate: `spendguard.external_standard_readiness:assess_external_standard_subset`.
  - **License / Security-Policy** (GATED-OFFLINE): Repository license and security policy files are required. Tracking: docs/READINESS.md.
  - **Token-Permissions** (GATED-OFFLINE): Every workflow defaults to contents: read and no workflow-wide write grants. Tracking: docs/READINESS.md.
  - **Dangerous-Workflow subset** (GATED-OFFLINE): Reject pull_request_target and unreviewed inline run expressions. The three permitted expressions are trusted runner output or configured numeric/version matrix data. Tracking: docs/READINESS.md.
  - **Pinned-Dependencies Actions subset** (GATED-OFFLINE): All remote Actions references require immutable 40-hex commits. Tracking: docs/READINESS.md.
  - **Binary-Artifacts** (REVIEWED-EXCEPTION): The vendored setuptools backend is a real binary-artifact finding; enforce the exact reviewed wheel SHA256, not a Scorecard pass. Tracking: docs/WAIVERS.md.
  - **Pinned-Dependencies remaining ecosystems** (REVIEWED-N/A): Full dependency pin coverage requires the Scorecard runner; this gate checks Actions only. Tracking: docs/READINESS-FOLLOWUP.md.
  - **Branch-Protection / Code-Review / Maintained / CI-Tests / Vulnerabilities / Fuzzing / Signed-Releases / Dependency-Update-Tool / CII-Best-Practices / Contributors / Packaging / SAST / Webhooks / License recognition** (REVIEWED-N/A): Requires live GitHub API, OSV, Scorecard runner, or external service state; file presence is not a score for these checks. Tracking: docs/READINESS-FOLLOWUP.md.

- [ ] [OpenSSF Best Practices Badge Passing mapping.](https://www.bestpractices.dev/en/criteria/0) — Reviewed offline subset only; no external certification claimed.
  Gate: `spendguard.external_standard_readiness:assess_external_standard_subset`.
  - **Repository evidence subset** (GATED-OFFLINE): Require license, README, contribution/security reporting documents, release notes, git, suite entrypoint and configured ruff + CodeQL. Tracking: docs/READINESS.md.
  - **All other Passing criteria and adequacy attestations** (REVIEWED-N/A): Live questionnaire, maintenance history, secure-development knowledge and document/process adequacy require maintainer attestation; repository files cannot certify them. Tracking: https://www.bestpractices.dev/en/criteria/0.

- [ ] [PyPA packaging standards mapping.](https://packaging.python.org/en/latest/tutorials/packaging-projects/) — Reviewed offline subset only; no external certification claimed.
  Gate: `spendguard.external_standard_readiness:assess_external_standard_subset`.
  - **Build / metadata / installed behavior** (GATED-OFFLINE): Build sdist and wheel offline with shipped backend provisioning; twine strict-check both; require core metadata/readme; import and version from a fresh venv with offline runtime dependency copied from this interpreter. Tracking: docs/READINESS.md.
  - **Publishing / index acceptance** (REVIEWED-N/A): PyPI credentials, network upload and index policy are external to offline artifact validity. Tracking: docs/READINESS-FOLLOWUP.md.

- [ ] [Command Line Interface Guidelines mapping.](https://clig.dev/) — Reviewed offline subset only; no external certification claimed.
  Gate: `spendguard.external_standard_readiness:assess_external_standard_subset`.
  - **Help / version / errors / plain output** (GATED-OFFLINE): Exercise help aliases, installed version, unknown-command stderr + suggestion and plain redirected NO_COLOR output; reuse complete real-flags guard. Tracking: docs/READINESS.md.
  - **Human usability / interactive behavior / remaining advice** (REVIEWED-N/A): Empathy, naming quality, interactive terminal UX, signal behavior and task-specific output design need human usability review; no all-command behavioral compliance claim. Tracking: docs/READINESS-FOLLOWUP.md.

- [ ] [Diátaxis documentation framework mapping.](https://diataxis.fr/start-here/) — Reviewed offline subset only; no external certification claimed.
  Gate: `spendguard.external_standard_readiness:assess_external_standard_subset`.
  - **Four discoverable quadrants** (GATED-OFFLINE): Existing index quickstart provides the guided install/govern/verify sequence; architecture explains, agent guide gives tasks, capability reference describes interfaces. LAUNCH is marketing planning, not a tutorial. Tracking: docs/READINESS.md.
  - **Pedagogical quality / quadrant purity** (REVIEWED-N/A): Learning effectiveness and information architecture quality require reader evaluation, not file or keyword checks. Tracking: docs/READINESS-FOLLOWUP.md.

## Architectural & code quality

Objective offline fitness functions enforce the recorded scope and measured ratchets. Reviewed exclusions are tracked explicitly.

- [ ] **architecture-import-cycles** — Track existing import cycles and reject new cyclic edges; complete acyclicity is reviewed N/A.
  Gate: `spendguard.architectural_fitness:run_architectural_fitness_gate`; inputs: `{"invariant": "cycles"}`.
  Review: AST includes deferred imports. Exact measured cyclic components and reviewed scope are recorded in architectural_fitness_baseline.json; removing existing cycles is tracked in docs/READINESS-FOLLOWUP.md. No claim of complete acyclicity.

- [ ] **architecture-context-boundaries** — Reject new cross-context imports that bypass the target context's declared surface.
  Gate: `spendguard.architectural_fitness:run_architectural_fitness_gate`; inputs: `{"invariant": "boundaries"}`.
  Review: The measured flat-module debt is a reviewed shrink-only ratchet; intra-context, declared shim-source, and surface-routed edges are allowed, while unassigned endpoints remain explicit unreviewed coverage.

- [ ] **architecture-no-new-flat-module** — Reject new top-level flat package modules while allowing reviewed legacy flat-module debt only to shrink.
  Gate: `spendguard.architectural_fitness:run_architectural_fitness_gate`; inputs: `{"invariant": "new_code"}`.
  Review: The measured direct-child Python module set is recorded as reviewed legacy debt. New code belongs inside a context package; nested modules are outside this mechanical placement ratchet.

- [ ] **architecture-layer-boundaries** — Application code may use the recorded standard-library-only foundations; foundations may not import application modules.
  Gate: `spendguard.architectural_fitness:run_architectural_fitness_gate`; inputs: `{"invariant": "layers"}`.
  Review: Actual foundation modules and rationale are baseline data. Full core/adapters/surfaces layering is reviewed N/A: config and pricing contain deferred imports into adapters/calls. Track broader decomposition in docs/READINESS-FOLLOWUP.md.

- [ ] **architecture-complexity-ratchet** — Radon cyclomatic complexity and maintainability index must not regress past measured per-file baselines.
  Gate: `spendguard.architectural_fitness:run_architectural_fitness_gate`; inputs: `{"invariant": "complexity"}`.
  Review: Every current file is independently measured for Python 3.11 and 3.12 in architectural_fitness_baseline.json. New files and unsupported Python minors require reviewed measurements; no invented grade threshold.

- [ ] **architecture-duplication-ratchet** — Pylint symilar duplicate significant lines and groups must not exceed measured current duplication.
  Gate: `spendguard.architectural_fitness:run_architectural_fitness_gate`; inputs: `{"invariant": "duplication"}`.
  Review: Package-wide current duplication and documented detector settings are baseline data. Existing clones are debt tracked in docs/READINESS-FOLLOWUP.md, not a zero-duplication claim.

- [ ] **architecture-dead-code** — Vulture definite unused variables and unreachable code require individually reviewed compatibility exceptions.
  Gate: `spendguard.architectural_fitness:run_architectural_fitness_gate`; inputs: `{"invariant": "dead"}`.
  Review: The recorded 100% certainty subset is gated across the package. Exact allowlist entries carry reasons. Lower-confidence dead functions and dynamic callers are reviewed N/A pending caller review in docs/READINESS-FOLLOWUP.md.

- [ ] **architecture-type-integrity** — Mypy checks the measured existing clean annotated surface without suppressing its diagnostics.
  Gate: `spendguard.architectural_fitness:run_architectural_fitness_gate`; inputs: `{"invariant": "types"}`.
  Review: The exact clean subset and isolation options are baseline data. Full-package type coverage is reviewed N/A and tracked in docs/READINESS-FOLLOWUP.md; py.typed is not evidence of clean typing.

- [ ] **architecture-name-uniqueness** — Existing unique-semantic naming fitness function; no duplicate implementation.
  Gate: `spendguard.capability_readiness_gates:run_capability_script_readiness_gate`; inputs: `{"scripts": ["tests/test_names_stay_unique.py"]}`.
  Review: Cites the existing registry-backed naming gate and docs/NAME_REGISTRY.json; collisions require an adjudicated verdict.

- [ ] **architecture-surface-parity** — Existing capability-surface-parity fitness function, also declared as surface-parity in Project invariants.
  Gate: `spendguard.capability_readiness_gates:run_capability_script_readiness_gate`; inputs: `{"scripts": ["tests/test_capability_surface_parity_guard.py"]}`.
  Review: Cites and reuses the existing parity gate; its projection/SSOT implementation is unchanged.


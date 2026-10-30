# Readiness

Definition revision: 1

Square means every checklist item has a reviewed deterministic gate and every gate passes.
`spendguard selfcheck` runs each gate and exits nonzero for FAIL or UNREVIEWED.
Coverage counts PASS and FAIL items with reviewed gates; UNREVIEWED counts as neither coverage nor success.
A supplied clean-environment report must match the current input fingerprint and its complete verification logs.

## Project invariants

- [ ] **surface-parity** — Every capability is exposed on CLI, API and MCP except declared deliberate absences.
  Gate: `spendguard.capability_readiness_gates:run_capability_script_readiness_gate`; inputs: `{"scripts": ["tests/test_capability_surface_parity_guard.py"]}`.

- [ ] **help-non-trivial** — Every CLI command exposes real flags or its explicit no-flags declaration.
  Gate: `spendguard.capability_readiness_gates:run_capability_script_readiness_gate`; inputs: `{"scripts": ["tests/test_capability_cli_help_guard.py"]}`.

- [ ] **docs-present** — Generated API reference and parameter contracts match the current registry.
  Gate: `spendguard.capability_readiness_gates:run_capability_script_readiness_gate`; inputs: `{"scripts": ["tests/test_capability_documentation_guard.py"]}`.

- [ ] **no-release-on-red-ci** — Release promotion fails closed when CI is red or unknown.
  Gate: `spendguard.capability_readiness_gates:run_capability_script_readiness_gate`; inputs: `{"scripts": ["tests/test_release_green_pointer.py"]}`.

- [ ] **env-parity-in-verification** — Current source passes required checks in independent clean environments.
  Gate: `spendguard.capability_readiness_gates:assess_capability_environment_evidence`; inputs: `{"patterns": ["src/spendguard/**/*.py", "src/spendguard/capability*.json", "src/spendguard/capability*.jsonl", "scripts/test/*.py", "tests/**/*.py", "pyproject.toml"], "required_checks": ["ruff", "name-uniqueness", "surface-suites", "offline-audit"]}`.
- [ ] **money-correctness** — Money scale, exact ledger arithmetic and global receipt allocation remain correct.
  Gate: `spendguard.capability_readiness_gates:run_capability_script_readiness_gate`; inputs: `{"scripts": ["tests/test_money_scale.py", "tests/test_ledger_money_is_exact_decimal.py", "tests/test_receipt_global_dollar_allocation.py"]}`.

## External standards — UNREVIEWED

These mappings are intentionally unreviewed stubs. They keep selfcheck red until reviewed and assigned gates.

- [ ] [OpenSSF Scorecard mapping.](https://scorecard.dev/) — UNREVIEWED: external standard mapping is deferred by the task.

- [ ] [OpenSSF Best Practices Badge mapping.](https://www.bestpractices.dev/) — UNREVIEWED: external standard mapping is deferred by the task.

- [ ] [PyPA packaging standards mapping.](https://packaging.python.org/) — UNREVIEWED: external standard mapping is deferred by the task.

- [ ] [Command Line Interface Guidelines mapping.](https://clig.dev/) — UNREVIEWED: external standard mapping is deferred by the task.

- [ ] [Diátaxis documentation framework mapping.](https://diataxis.fr/) — UNREVIEWED: external standard mapping is deferred by the task.

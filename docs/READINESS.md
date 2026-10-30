# Readiness

Definition revision: 2

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
  Gate: `spendguard.capability_readiness_gates:assess_capability_environment_evidence`; inputs: `{"patterns": ["src/spendguard/**/*.py", "src/spendguard/capability*.json", "src/spendguard/capability*.jsonl", "scripts/test/*.py", "tests/**/*.py", "pyproject.toml", ".github/workflows/*", "docs/*.md", "LICENSE", "SECURITY.md", "CONTRIBUTING.md", "README.md", "CHANGELOG.md", "src/spendguard/distribution_build_wheels/*.whl"], "required_checks": ["ruff", "name-uniqueness", "surface-suites", "offline-audit"]}`.

- [ ] **money-correctness** — Money scale, exact ledger arithmetic and global receipt allocation remain correct.
  Gate: `spendguard.capability_readiness_gates:run_capability_script_readiness_gate`; inputs: `{"scripts": ["tests/test_money_scale.py", "tests/test_ledger_money_is_exact_decimal.py", "tests/test_receipt_global_dollar_allocation.py"]}`.

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

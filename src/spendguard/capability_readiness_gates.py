"""Deterministic readiness gates with explicit evidence and no inferred success."""
import hashlib
import json
from pathlib import Path
import runpy
import sys

from .capability_readiness import CapabilityReadinessEvidence

READINESS_HASH_CHUNK_BYTES = 1024 * 1024
READINESS_REQUIRED_ENVIRONMENTS = 2
READINESS_SCRIPT_HARNESS_PATH = Path("scripts") / "test" / "chunked_suite.py"


def capability_verification_fingerprint(repo_root, patterns):
    """Hash every declared verification input; a missing pattern fails closed."""
    root = Path(repo_root)
    paths = set()
    for pattern in patterns:
        matches = {path for path in root.glob(pattern) if path.is_file()}
        if not matches:
            raise FileNotFoundError(f"Verification input pattern has no files: {pattern}")
        paths.update(matches)
    digest = hashlib.sha256()
    for path in sorted(paths):
        digest.update(path.relative_to(root).as_posix().encode())
        digest.update(b"\0")
        with path.open("rb") as stream:
            while chunk := stream.read(READINESS_HASH_CHUNK_BYTES):
                digest.update(chunk)
        digest.update(b"\0")
    return digest.hexdigest()


def run_capability_script_readiness_gate(*, context, scripts):
    """Execute complete existing checks under this interpreter, retaining exit evidence."""
    root = context["repo_root"]
    harness_path = root / READINESS_SCRIPT_HARNESS_PATH
    if not harness_path.is_file():
        return CapabilityReadinessEvidence("UNREVIEWED", f"Isolated check harness unavailable: {harness_path}")
    # Reuse the canonical runner's startup isolation, credential stripping,
    # offline enforcement and cleanup rather than launching against user state.
    harness = runpy.run_path(str(harness_path))
    run_isolated_script = harness["_run_one"]
    _, runner_budget_scale = harness["measure_isolated_runner_budget_scale"]()
    evidence = []
    failed = []
    for relative_path in scripts:
        script = root / relative_path
        if not script.is_file():
            return CapabilityReadinessEvidence("UNREVIEWED", f"Check unavailable: {relative_path}")
        returncode, stdout, stderr = run_isolated_script(str(script), runner_budget_scale)
        evidence.append(f"{relative_path}: exit {returncode}\nstdout:\n{stdout}\nstderr:\n{stderr}")
        if returncode:
            failed.append(f"{relative_path}\n{stdout}\n{stderr}")
    if not evidence:
        return CapabilityReadinessEvidence("UNREVIEWED", "No checks declared")
    return CapabilityReadinessEvidence(
        "FAIL" if failed else "PASS",
        "\n".join(failed) if failed else "All declared checks passed under " + sys.version.split()[0],
        tuple(evidence),
    )


def assess_capability_environment_evidence(*, context, patterns, required_checks):
    """Require current-input, complete successful checks from independent clean environments."""
    report_path = context.get("environment_report")
    if report_path is None:
        return CapabilityReadinessEvidence("UNREVIEWED", "No clean-environment verification report supplied")
    report = json.loads(Path(report_path).read_text(encoding="utf-8"))
    current_fingerprint = capability_verification_fingerprint(context["repo_root"], patterns)
    if report.get("fingerprint") != current_fingerprint:
        return CapabilityReadinessEvidence("UNREVIEWED", "Verification report does not cover current source inputs")
    environments = report.get("environments", [])
    identities = {environment.get("interpreter") for environment in environments}
    if len(identities) < READINESS_REQUIRED_ENVIRONMENTS:
        return CapabilityReadinessEvidence("UNREVIEWED", "Independent clean-interpreter verification is incomplete")
    evidence = []
    for environment in environments:
        if environment.get("clean") is not True:
            return CapabilityReadinessEvidence("UNREVIEWED", "Environment lacks an explicit clean-install declaration")
        checks = environment.get("checks", {})
        if any(check not in checks for check in required_checks):
            return CapabilityReadinessEvidence("UNREVIEWED", "Environment report omits required checks")
        if any(checks[check]["exit_code"] != 0 for check in required_checks):
            return CapabilityReadinessEvidence("FAIL", "A clean environment reported a failing required check")
        for check in required_checks:
            record = checks[check]
            log_path = Path(record["log"])
            if not log_path.is_file():
                return CapabilityReadinessEvidence("UNREVIEWED", f"Verification log unavailable: {log_path}")
            if hashlib.sha256(log_path.read_bytes()).hexdigest() != record["sha256"]:
                return CapabilityReadinessEvidence("FAIL", f"Verification log changed: {log_path}")
            evidence.append(f"{environment['interpreter']} {check}: {log_path}")
    return CapabilityReadinessEvidence("PASS", "Current inputs passed all declared checks in clean environments", tuple(evidence))

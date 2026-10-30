"""Record real offline verification logs and merge transported clean-environment evidence."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

from spendguard.capability_readiness import load_capability_readiness_manifest
from spendguard.capability_readiness_gates import capability_verification_fingerprint
from spendguard.capability_readiness_gates import run_capability_script_readiness_gate

READINESS_ENVIRONMENT_ITEM = "env-parity-in-verification"
READINESS_SURFACE_ITEMS = ("surface-parity", "help-non-trivial", "docs-present")
READINESS_ENVIRONMENT_RECORD = "environment.json"
READINESS_ENVIRONMENT_LOG_SUFFIX = ".log"


def record_readiness_environment(repo_root, output_directory, clean_install_report):
    """Run the declared checks, retaining complete bytes and actual return codes."""
    import spendguard
    spendguard.require()
    if sys.prefix == sys.base_prefix:
        raise RuntimeError("Clean-environment evidence requires a dedicated virtual environment")
    installation_provenance = json.loads(clean_install_report.read_text())
    if (installation_provenance.get("interpreter") != sys.executable
            or installation_provenance.get("clean") is not True
            or installation_provenance.get("installation_exit_code") != 0):
        raise ValueError("Fresh-install provenance does not attest this interpreter's successful clean installation")
    _, readiness_items = load_capability_readiness_manifest()
    environment_item = next(item for item in readiness_items if item.id == READINESS_ENVIRONMENT_ITEM)
    surface_scripts = [script for item in readiness_items if item.id in READINESS_SURFACE_ITEMS
                       for script in item.arguments["scripts"]]
    check_commands = {
        "ruff": [sys.executable, "-m", "ruff", "check", "src", "tests"],
        "offline-audit": [sys.executable, "-m", "spendguard", "audit", "--ci"],
    }
    script_checks = {"name-uniqueness": ["tests/test_names_stay_unique.py"], "surface-suites": surface_scripts}
    # The manifest is authoritative: adding a required check cannot silently produce a pass.
    if set(check_commands) | set(script_checks) != set(environment_item.arguments["required_checks"]):
        raise ValueError("Environment evidence commands do not cover the manifest's required checks")
    output_directory.mkdir(parents=True, exist_ok=True)
    recorded_checks = {}
    for check_name, check_command in check_commands.items():
        log_name = check_name + READINESS_ENVIRONMENT_LOG_SUFFIX
        log_path = output_directory / log_name
        with log_path.open("wb") as log_stream:
            check_environment = dict(os.environ, SPENDGUARD_AUDIT_DIR=str(repo_root / "src" / "spendguard"))
            result = subprocess.run(check_command, cwd=repo_root, env=check_environment, stdout=log_stream,
                                    stderr=subprocess.STDOUT, check=False)
        recorded_checks[check_name] = {
            "exit_code": result.returncode, "log": log_name,
            "sha256": hashlib.sha256(log_path.read_bytes()).hexdigest(),
        }
        print(f"{check_name}: exit {result.returncode}; full log {log_path}", flush=True)
    for check_name, scripts in script_checks.items():
        script_result = run_capability_script_readiness_gate(context={"repo_root": repo_root}, scripts=scripts)
        log_path = output_directory / (check_name + READINESS_ENVIRONMENT_LOG_SUFFIX)
        log_path.write_text("\n".join(script_result.evidence) + "\n" + script_result.detail + "\n")
        recorded_checks[check_name] = {
            "exit_code": int(script_result.status != "PASS"), "log": log_path.name,
            "sha256": hashlib.sha256(log_path.read_bytes()).hexdigest(),
        }
        print(f"{check_name}: {script_result.status}; full log {log_path}", flush=True)
    report = {
        "fingerprint": capability_verification_fingerprint(repo_root, environment_item.arguments["patterns"]),
        "environments": [{"interpreter": sys.version, "clean": True,
                          "installation_provenance": installation_provenance,
                          "checks": recorded_checks}],
    }
    (output_directory / READINESS_ENVIRONMENT_RECORD).write_text(json.dumps(report, indent=2) + "\n")
    return int(any(check["exit_code"] for check in recorded_checks.values()))


def merge_readiness_environment_records(input_directory, output_path):
    """Relocate log references after artifact transport without rewriting their hashes."""
    report_paths = sorted(input_directory.rglob(READINESS_ENVIRONMENT_RECORD))
    if not report_paths:
        raise ValueError("No environment evidence artifacts found")
    merged_report = {"fingerprint": None, "environments": []}
    for report_path in report_paths:
        source_report = json.loads(report_path.read_text())
        if merged_report["fingerprint"] is None:
            merged_report["fingerprint"] = source_report["fingerprint"]
        if merged_report["fingerprint"] != source_report["fingerprint"]:
            raise ValueError("Environment artifacts cover different source inputs")
        for environment in source_report["environments"]:
            for check in environment["checks"].values():
                log_path = (report_path.parent / check["log"]).resolve()
                if hashlib.sha256(log_path.read_bytes()).hexdigest() != check["sha256"]:
                    raise ValueError(f"Transported evidence log changed: {log_path}")
                check["log"] = str(log_path)
            merged_report["environments"].append(environment)
    output_path.write_text(json.dumps(merged_report, indent=2) + "\n")


def readiness_environment_evidence_main():
    evidence_parser = argparse.ArgumentParser(description=__doc__)
    evidence_parser.add_argument("mode", choices=("record", "merge"))
    evidence_parser.add_argument("--repo-root", type=Path, default=Path.cwd())
    evidence_parser.add_argument("--input", type=Path)
    evidence_parser.add_argument("--output", type=Path, required=True)
    evidence_parser.add_argument("--clean-install-report", type=Path)
    evidence_arguments = evidence_parser.parse_args()
    if evidence_arguments.mode == "record":
        if evidence_arguments.clean_install_report is None:
            evidence_parser.error("record requires --clean-install-report from a successful fresh installation")
        return record_readiness_environment(evidence_arguments.repo_root.resolve(), evidence_arguments.output,
                                            evidence_arguments.clean_install_report)
    if evidence_arguments.input is None:
        evidence_parser.error("merge requires --input")
    merge_readiness_environment_records(evidence_arguments.input, evidence_arguments.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(readiness_environment_evidence_main())

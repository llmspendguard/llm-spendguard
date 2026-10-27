"""Offline guards for registry-derived LLM capability detection and ledger×code cross-checking."""
import contextlib
import io
import os
from pathlib import Path
import sys
import tempfile
from unittest import mock

if not os.environ.get("SPENDGUARD_TEST_ISOLATED"):
    os.environ["SPENDGUARD_TEST_ISOLATED"] = "1"
    os.environ["SPENDGUARD_NO_AUTOINSTALL"] = "1"
    os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="spendguard-coverage-crosscheck-")
    os.execv(sys.executable, [sys.executable] + sys.argv)

from _script_checks import record_script_check  # noqa: E402

from spendguard import adapters, consumer_audit, coverage
from spendguard.setup import _PTH_SENTINEL, _SENTINEL

failures = []


with tempfile.TemporaryDirectory(prefix="coverage-consumer-") as directory:
    source = os.path.join(directory, "consumer.py")
    with open(source, "w") as handle:
        handle.write("import spendguard\n")
    facts = consumer_audit.repository_import_facts(Path(directory))
    record_script_check(failures, "spendguard-only fixture proves old sdk-only result was false", bool(facts["sdk"]) is False)
    llm_calling = bool(facts["sdk"]) or facts["spendguard_import"]
    record_script_check(failures, "spendguard-only fixture is LLM-calling", llm_calling is True)
    verdict = consumer_audit._consumer_verdict(
        llm_calling, {"confirmed": True, "spendguard_imports": True,
                      "gate_enforcing": True, "sha_relation": "=="}, "deployed")
    record_script_check(failures, "spendguard-only verdict is not NOT-LLM", verdict != consumer_audit.CONSUMER_AUDIT_NOT_LLM)

with tempfile.TemporaryDirectory(prefix="coverage-native-sdk-") as directory:
    with open(os.path.join(directory, "consumer.py"), "w") as handle:
        handle.write("import google.generativeai\n")
    facts = consumer_audit.repository_import_facts(Path(directory))
    record_script_check(failures, "native SDK import is LLM-calling", facts["sdk"] == ("google",))

with tempfile.TemporaryDirectory(prefix="coverage-registry-") as directory:
    provider_name = "registry-fixture-provider"
    sdk_import = "registry_fixture_sdk"
    provider = {"base_url": None, "key_env": "REGISTRY_FIXTURE_KEY", "prefixes": (),
                "kind": "openai", "sdk_import": sdk_import}
    with mock.patch.dict(adapters.PROVIDERS, {provider_name: provider}):
        record_script_check(failures, "registry helper includes added native SDK", sdk_import in adapters.llm_capability_import_names())
        with open(os.path.join(directory, "consumer.py"), "w") as handle:
            handle.write("import %s\n" % sdk_import)
        record_script_check(failures, "consumer detector follows registry without detector edit",
                                consumer_audit.repository_import_facts(Path(directory))["sdk"] == (sdk_import,))
        venv = os.path.join(directory, "venv")
        purelib = os.path.join(venv, "site-packages")
        os.makedirs(os.path.join(purelib, sdk_import))
        with mock.patch.object(coverage, "_purelib", return_value=purelib):
            rows = coverage.audit_gating_coverage(roots=[venv])
        record_script_check(failures, "coverage detector follows registry without detector edit", rows[0]["llm"] == [sdk_import])

with tempfile.TemporaryDirectory(prefix="coverage-sentinel-") as directory:
    purelib = os.path.join(directory, "site-packages")
    os.makedirs(purelib)
    hook = os.path.join(purelib, "sitecustomize.py")
    for sentinel in (_SENTINEL, _PTH_SENTINEL):
        with open(hook, "w") as handle:
            handle.write(sentinel)
        record_script_check(failures, "hook sentinel accepted: %s" % sentinel, coverage._is_gated(directory, purelib) is True)
    with open(hook, "w") as handle:
        handle.write("import spendguard\n")
    record_script_check(failures, "hook without either sentinel is rejected", coverage._is_gated(directory, purelib) is False)

with tempfile.TemporaryDirectory(prefix="coverage-spendguard-dir-") as directory:
    venv = os.path.join(directory, "venv")
    purelib = os.path.join(venv, "site-packages")
    os.makedirs(os.path.join(purelib, adapters.SPENDGUARD_IMPORT_NAME))
    with mock.patch.object(coverage, "_purelib", return_value=purelib):
        rows = coverage.audit_gating_coverage(roots=[venv])
    record_script_check(failures, "spendguard package directory is LLM-capable",
                            rows[0]["llm"] == [adapters.SPENDGUARD_IMPORT_NAME])


def crosscheck_output(rows, captured_sources):
    output = io.StringIO()
    with mock.patch.object(coverage, "audit_gating_coverage", return_value=rows), \
            mock.patch.object(coverage, "_captured_spend_sources", return_value=captured_sources), \
            contextlib.redirect_stdout(output):
        coverage.cmd()
    return output.getvalue()


try:
    isolated_captured_sources = coverage._captured_spend_sources()
except Exception as captured_sources_error:
    record_script_check(failures, "isolated captured-spend seam returns a list", False)
    print("    %s: %s" % (type(captured_sources_error).__name__, captured_sources_error))
else:
    record_script_check(failures, "isolated captured-spend seam returns a list", isinstance(isolated_captured_sources, list))


with tempfile.TemporaryDirectory(prefix="coverage-cells-") as directory:
    repo = os.path.join(directory, "repo")
    venv = os.path.join(repo, ".venv")
    row = {"venv": venv, "llm": ["spendguard"], "gated": True, "own": False}
    no_source_truth = crosscheck_output([row], [])
    record_script_check(failures, 
        "empty captured sources print source-attribution advisory",
        "ledger×code cross-check skipped — no source-path attribution in the ledger" in no_source_truth,
    )
    record_script_check(failures, "empty captured sources do not print DANGER",
                            "LLM-capable but no captured spend" not in no_source_truth)
    record_script_check(failures, "empty captured sources do not print CONTRADICTION",
                            "captured spend but no LLM code path found" not in no_source_truth)
    uncovered_source = os.path.join(directory, "uncovered", "source")
    danger = crosscheck_output([row], [uncovered_source])
    record_script_check(failures, "code-capable/nonmatching-spend prints DANGER",
                            "LLM-capable but no captured spend" in danger)
    contradiction = crosscheck_output([], [repo])
    record_script_check(failures, "captured-spend/no-code prints CONTRADICTION",
                            "captured spend but no LLM code path found" in contradiction)
    record_script_check(failures, "CONTRADICTION fixture does not print DANGER", "LLM-capable but no captured spend" not in contradiction)
    matched = crosscheck_output([row], [repo])
    record_script_check(failures, "matched pair prints neither off-diagonal",
                            "LLM-capable but no captured spend" not in matched
                            and "captured spend but no LLM code path found" not in matched)

with tempfile.TemporaryDirectory(prefix="coverage-cmd-seam-") as directory:
    code_venv = os.path.join(directory, "code", ".venv")
    ledger_source = os.path.join(directory, "ledger", "source")
    row = {"venv": code_venv, "llm": ["spendguard"], "gated": True, "own": False}
    rendered_crosscheck = crosscheck_output([row], [ledger_source])
    record_script_check(failures, "cmd renders LLM-capable source missing captured spend",
                            "LLM-capable but no captured spend" in rendered_crosscheck)
    record_script_check(failures, "cmd renders captured spend missing an LLM code path",
                            "captured spend but no LLM code path found" in rendered_crosscheck)
    record_script_check(failures, "cmd renders cross-check instead of unavailable fallback",
                            "ledger×code cross-check unavailable" not in rendered_crosscheck)

print(("[OK]" if not failures else "[FAIL]") + " coverage capability cross-check: %d failure(s)" % len(failures))
sys.exit(1 if failures else 0)

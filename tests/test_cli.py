"""CLI router (cli.main) dispatch — the single point every command flows through, previously untested. Stubs the
dispatch targets; no network/LLM. Isolated SPENDGUARD_HOME."""
import os, pathlib, sys, tempfile

if not os.environ.get("SPENDGUARD_TEST_ISOLATED"):
    os.environ["SPENDGUARD_TEST_ISOLATED"] = "1"
    os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="spendguard-test-")
    os.execv(sys.executable, [sys.executable] + sys.argv)

from _script_checks import record_script_check  # noqa: E402

from spendguard import cli, fleet, gate, reconcile, resources

fails = []
gate.install = lambda: None                                # don't patch live SDKs in the test
seen = {}
reconcile.report = lambda *a, **k: seen.__setitem__("reconcile.report", True)
resources.cmd = lambda rest=None: (seen.__setitem__("resources.cmd", list(rest or [])), 0)[1]
fleet.deregister_interpreter = lambda executable: (
    seen.__setitem__("fleet.deregister_interpreter", executable)
    or {"removed": True, "python_executable": executable, "remaining": 0})

record_script_check(fails, "`reconcile all` → reconcile.report, returns 0", cli.main(["reconcile", "all"]) == 0 and seen.get("reconcile.report"))
record_script_check(fails, "`resources discover` → resources.cmd(['discover'])", cli.main(["resources", "discover"]) == 0 and seen.get("resources.cmd") == ["discover"])
record_script_check(fails, "`resources sync` → resources.cmd(['sync'])", cli.main(["resources", "sync"]) == 0 and seen.get("resources.cmd") == ["sync"])
fleet_test_python = str(pathlib.Path(tempfile.mkdtemp(prefix="spendguard-cli-fleet-")) / "python")
record_script_check(fails, "`fleet deregister <python>` explicitly retires that interpreter",
   cli.main(["fleet", "deregister", fleet_test_python]) == 0
   and seen.get("fleet.deregister_interpreter") == fleet_test_python)
record_script_check(fails, "`fleet deregister` without exactly one interpreter is a usage error",
   cli.main(["fleet", "deregister"]) == 2)
# Unknown command now exits 2 — the shell convention for a USAGE error — with a did-you-mean, instead of the old
# exit-1 dump of a 9-line docstring. Explicit help/version exit 0 (they are not errors). Details in
# tests/test_deferred_fixes.py.
record_script_check(fails, "unknown command → exit 2 (usage error), no crash", cli.main(["totally-bogus-xyz"]) == 2)
record_script_check(fails, "`--help` exits 0 (an explicit help request is not an error)", cli.main(["--help"]) == 0)
record_script_check(fails, "`--version` exits 0", cli.main(["--version"]) == 0)

print(("\n[FAIL] " if fails else "\n[OK] ") + f"cli: {len(fails)} failure(s)")
sys.exit(1 if fails else 0)

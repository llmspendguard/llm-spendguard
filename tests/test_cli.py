"""CLI router (cli.main) dispatch — the single point every command flows through, previously untested. Stubs the
dispatch targets; no network/LLM. Isolated SPENDGUARD_HOME."""
import os, sys, tempfile

if not os.environ.get("SPENDGUARD_TEST_ISOLATED"):
    os.environ["SPENDGUARD_TEST_ISOLATED"] = "1"
    os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="spendguard-test-")
    os.execv(sys.executable, [sys.executable] + sys.argv)

from spendguard import cli, gate, reconcile, resources

fails = []
def ck(name, cond):
    print(("  [OK] " if cond else "  [FAIL] ") + name)
    if not cond:
        fails.append(name)

gate.install = lambda: None                                # don't patch live SDKs in the test
seen = {}
reconcile.report = lambda *a, **k: seen.__setitem__("reconcile.report", True)
resources.cmd = lambda rest=None: (seen.__setitem__("resources.cmd", list(rest or [])), 0)[1]

ck("`reconcile all` → reconcile.report, returns 0", cli.main(["reconcile", "all"]) == 0 and seen.get("reconcile.report"))
ck("`resources discover` → resources.cmd(['discover'])", cli.main(["resources", "discover"]) == 0 and seen.get("resources.cmd") == ["discover"])
ck("`resources sync` → resources.cmd(['sync'])", cli.main(["resources", "sync"]) == 0 and seen.get("resources.cmd") == ["sync"])
ck("unknown command → returns 1 (prints help, no crash)", cli.main(["totally-bogus-xyz"]) == 1)

print(("\n[FAIL] " if fails else "\n[OK] ") + f"cli: {len(fails)} failure(s)")
sys.exit(1 if fails else 0)

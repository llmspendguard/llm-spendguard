"""spendguard.__version__ must track pyproject.toml — it was a hardcoded literal that shipped as "0.3.0"
for four releases. Now it reads installed package metadata (single source); this guard fails if the two
ever drift again (installed/editable) or the dunder goes missing (source-tree fallback is 0.0.0.dev0).
NOTE for editable venvs: metadata freezes at install time, so after a version bump run
`pip install -e . --no-deps` to refresh dist-info — this guard failing right after a bump means exactly that.
"""
import os, re, sys, tempfile
if not os.environ.get("SPENDGUARD_TEST_ISOLATED"):
    os.environ["SPENDGUARD_TEST_ISOLATED"] = "1"
    os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="spendguard-ver-")
    os.execv(sys.executable, [sys.executable] + sys.argv)

import spendguard

failures = 0
def check(label, cond):
    global failures
    ok = bool(cond)
    if not ok: failures += 1
    print(f"  [{'OK' if ok else 'FAIL'}] {label}")


pyproject = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "pyproject.toml")
m = re.search(r'^version\s*=\s*"([^"]+)"', open(pyproject).read(), re.M)
check("pyproject version parseable", bool(m))
declared = m.group(1) if m else "?"
check("__version__ exists", hasattr(spendguard, "__version__"))
check(f"__version__ ({spendguard.__version__}) == pyproject ({declared}) — or the explicit dev fallback",
      spendguard.__version__ in (declared, "0.0.0.dev0"))
check("the pre-fix stale literal can never come back", spendguard.__version__ != "0.3.0" or declared == "0.3.0")

print(f"\n{'[FAIL]' if failures else 'OK'} test_version_dunder: {failures} failure(s)")
sys.exit(1 if failures else 0)

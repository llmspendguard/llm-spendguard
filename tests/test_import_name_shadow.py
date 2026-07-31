"""The `spendguard` IMPORT NAME can be shadowed — report it QUIETLY, in the diagnostic command.

Naming is settled and not up for re-litigation: dist/PyPI **llm-spendguard**, import + CLI **spendguard**, domain
llmspendguard.com. But an UNRELATED PyPI project named `spendguard` (0.1.0, published 2026-06-28) ships a
top-level `spendguard` module too — verified by inspecting its wheel. Two distributions providing one import name
means whichever installed last wins, silently. Worth knowing — but NOT worth an ambient stderr warning on
every interpreter start: the first version of this check shouted during unrelated work in another repo, for what
turned out to be a stale build artifact. Diagnostics belong in `spendguard doctor`, not in everyone's stderr.

The first run of this check found a REAL problem: a stale `src/spendguard.egg-info` left over from before the
v0.2.0 rename was still claiming the name in the dev tree. Deleted; this test keeps it deleted.
"""
import os, sys, tempfile

if not os.environ.get("SPENDGUARD_TEST_ISOLATED"):
    os.environ["SPENDGUARD_TEST_ISOLATED"] = "1"
    os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="spendguard-shadow-")
    os.execv(sys.executable, [sys.executable] + sys.argv)

import pathlib
import spendguard

failures = 0
def check(label, cond):
    global failures
    ok = bool(cond)
    if not ok: failures += 1
    print(f"  [{'OK' if ok else 'FAIL'}] {label}")


print("-- this environment is not shadowed --")
dists = spendguard.which_package()
others = [d for d in dists if d.replace("_", "-").lower() != "llm-spendguard"]
check(f"only llm-spendguard provides the `spendguard` import name (saw {dists})", not others)
check("which_package() is exposed for users to check their own env", callable(spendguard.which_package))

print("-- the stale pre-rename egg-info stays deleted (it claimed the name in the source tree) --")
root = pathlib.Path(spendguard.__file__).parent.parent.parent
stale = root / "src" / "spendguard.egg-info"
check(f"no {stale.name} in src/ (a build artifact from the dist rename)", not stale.exists())

print("-- import is SILENT: no ambient warning, ever --")
import io
import contextlib
import subprocess
buf = io.StringIO()
with contextlib.redirect_stderr(buf):
    import importlib
    importlib.reload(spendguard)
check("reloading the package prints nothing to stderr", buf.getvalue() == "")
r = subprocess.run([sys.executable, "-c", "import spendguard"], capture_output=True, text=True)
check("a fresh `import spendguard` in a clean process is silent", r.stderr.strip() == "")
check("no ambient warn helper survives in the package", not hasattr(spendguard, "_warn_if_shadowed"))
src = open(spendguard.__file__).read()
check("__init__ writes nothing to stderr at import", "stderr.write" not in src)

print("-- silence holds on Python 3.9 too (our own minimum), where entry_points() takes no kwargs --")
import importlib.metadata as _md
from spendguard import provider_plugins as _pp
_real_eps = _md.entry_points


def _legacy_eps(*a, **k):                       # the 3.9 API: no kwargs, returns {group: [ep, ...]}
    if k:
        raise TypeError("entry_points() got an unexpected keyword argument 'group'")
    return {_pp.GROUP: []}


_md.entry_points = _legacy_eps
_buf = io.StringIO()
try:
    with contextlib.redirect_stderr(_buf):
        _pp.load()
finally:
    _md.entry_points = _real_eps
check("plugin discovery is silent under the 3.9 entry_points API (it WARNed on every import)",
      _buf.getvalue() == "")

print("-- the check is exposed for the DIAGNOSTIC command instead --")
check("shadowing_dists() is the queryable API", callable(spendguard.shadowing_dists))
check("it reports clean for this environment", spendguard.shadowing_dists() == [])
real = spendguard.which_package
spendguard.which_package = lambda: ["llm-spendguard", "spendguard"]
check("a foreign dist IS detected when asked", spendguard.shadowing_dists() == ["spendguard"])
spendguard.which_package = lambda: ["llm-spendguard", "LLM_SpendGuard"]
check("name normalization (case/underscore) does not false-positive", spendguard.shadowing_dists() == [])
spendguard.which_package = real
from spendguard import gate
import inspect
check("`spendguard doctor` is where it surfaces", "shadowing_dists" in inspect.getsource(gate._cli))

print(f"\n{'[FAIL]' if failures else 'OK'} test_import_name_shadow: {failures} failure(s)")
sys.exit(1 if failures else 0)

"""spendguard.__version__ must be the version of the CODE that is actually running, single-sourced.

History: it was a hardcoded literal that shipped as "0.3.0" for four releases; the fix moved it to
importlib.metadata — but that reads INSTALL-TIME metadata, which under an editable install freezes at install time,
so it reported 0.7.2 while running 0.10.0 code (warden 2026-09-25, the "packaging version lie"). The durable fix is
a real single source: src/spendguard/_version.py, read live by __init__ and consumed by pyproject at build time.

This guard fails if any of that regresses: if __version__ stops tracking the _version SSOT, if a second reader
(measurement._sg_version) drifts from it, if pyproject reintroduces a static literal or stops deriving from the
SSOT, or if the pre-fix stale literal comes back.
"""
import os, sys, tempfile
if not os.environ.get("SPENDGUARD_TEST_ISOLATED"):
    os.environ["SPENDGUARD_TEST_ISOLATED"] = "1"
    os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="spendguard-ver-")
    os.execv(sys.executable, [sys.executable] + sys.argv)

import spendguard
from spendguard import _version, measurement

_fails = []
def check(label, cond):
    if not cond:
        _fails.append(label)
    print(f"  [{'OK' if cond else 'FAIL'}] {label}")


ssot = _version.__version__
check("_version.__version__ is a real version, not the dev fallback / unknown",
      isinstance(ssot, str) and ssot not in ("", "0.0.0.dev0", "?"))
check("spendguard.__version__ exists", hasattr(spendguard, "__version__"))
check(f"spendguard.__version__ ({getattr(spendguard, '__version__', None)}) reads the _version SSOT ({ssot})",
      spendguard.__version__ == ssot)
check(f"measurement._sg_version() ({measurement._sg_version()}) tracks the SSOT — no second frozen-metadata read",
      measurement._sg_version() == ssot)
check("the pre-fix stale literal can never come back", spendguard.__version__ != "0.3.0" or ssot == "0.3.0")

# __init__ must NOT go back to reading importlib.metadata for its own version (the editable-install lie).
init_src = open(os.path.join(os.path.dirname(_version.__file__), "__init__.py")).read()
check("__init__ does not read importlib.metadata for __version__ (reads the SSOT instead)",
      "from ._version import __version__" in init_src and "_pkg_version(" not in init_src)

# pyproject must be dynamic (no static literal) and derive the version from the SSOT at build time.
pyproject_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "pyproject.toml")
raw = open(pyproject_path).read()
try:
    import tomllib
    pp = tomllib.loads(raw)
    proj = pp.get("project", {})
    check("pyproject [project].version is DYNAMIC (no static literal)",
          "version" in proj.get("dynamic", []) and "version" not in proj)
    attr = (pp.get("tool", {}).get("setuptools", {}).get("dynamic", {}).get("version", {}) or {}).get("attr")
    check(f"pyproject derives the version from the SSOT attr (got {attr!r})",
          attr == "spendguard._version.__version__")
except ModuleNotFoundError:                      # tomllib is py3.11+; fall back to a mechanical presence check
    check("pyproject declares dynamic version (no static literal)",
          'dynamic = ["version"]' in raw and "\nversion = " not in raw)
    check("pyproject derives the version from the SSOT attr",
          'attr = "spendguard._version.__version__"' in raw)

print(f"\n{'[FAIL]' if _fails else 'OK'} test_version_dunder: {len(_fails)} failure(s)")
sys.exit(1 if _fails else 0)

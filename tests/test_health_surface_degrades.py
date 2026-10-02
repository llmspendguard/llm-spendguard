"""The reachability surface DEGRADES, never crashes — the thing you call to learn what is healthy must not be the
thing that breaks.

Regression for the reported bug: `spendguard_health` returned {"error": "AttributeError: module 'spendguard.pricing'
has no attribute 'OUTPUT_FLOOR'"} because `adapters.TOKEN_FLOOR = pricing.OUTPUT_FLOOR` is read at MODULE LOAD, so a
skewed install (newer adapters, older pricing) hard-failed `import adapters` and took down the WHOLE surface. This
locks three things:
  1. sweep(run=False) returns a STRUCTURED result (estimate/lanes/metered) — the end-to-end call that would have
     caught the import crash ($0, no probes).
  2. a stale pricing with NO OUTPUT_FLOOR no longer AttributeErrors at adapters import — it degrades to the 32K floor.
  3. _tool_health returns a structured error result, not a raw exception, when the sweep errors.

Offline, isolated SPENDGUARD_HOME, zero spend (run=False + stubs).
"""
import os
import sys
import tempfile
import importlib

# Isolate BEFORE importing spendguard (config.HOME is read at import); no re-exec needed for this read-only test.
os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="sg-healthdegrade-")
os.environ.setdefault("SPENDGUARD_TEST_ISOLATED", "1")
os.environ.setdefault("SPENDGUARD_NO_AUTOINSTALL", "1")

from spendguard import reliability, pricing, adapters, mcp_server  # noqa: E402


class Checks:
    def __init__(self):
        self.fails = []

    def __call__(self, label, cond, extra=""):
        if not cond:
            self.fails.append(label)
        print(f"  [{'OK' if cond else 'FAIL'}] {label}{('  — ' + extra) if extra and not cond else ''}")


ck = Checks()

# ── 1. sweep(run=False) returns a STRUCTURED result end-to-end (exercises the plan/import path that crashed) ─────────
r = reliability.sweep(run=False)
ck("sweep(run=False) returns a dict with estimate/lanes/metered",
   isinstance(r, dict) and all(k in r for k in ("estimate", "lanes", "metered")),
   extra=repr(sorted(r)) if isinstance(r, dict) else type(r))

# ── 2. REGRESSION: a stale pricing (no OUTPUT_FLOOR) must NOT AttributeError at adapters import — degrade to 32K ──────
_saved = pricing.OUTPUT_FLOOR
try:
    delattr(pricing, "OUTPUT_FLOOR")
    importlib.reload(adapters)                 # re-run adapters' module body WITHOUT pricing.OUTPUT_FLOOR
    ck("stale pricing (no OUTPUT_FLOOR) does NOT crash adapters import", True)
    ck("TOKEN_FLOOR degrades to the 32K floor default", adapters.TOKEN_FLOOR == 32000, extra=repr(adapters.TOKEN_FLOOR))
except AttributeError as e:
    ck("stale pricing (no OUTPUT_FLOOR) does NOT crash adapters import", False, extra=repr(e))
finally:
    pricing.OUTPUT_FLOOR = _saved
    importlib.reload(adapters)                 # restore the real binding for anything after this test

# ── 3. _tool_health DEGRADES on a sweep error — a structured result with a populated error field, never a raw crash ──
_orig_sweep = reliability.sweep


def _boom(**_kw):
    raise AttributeError("module 'spendguard.pricing' has no attribute 'OUTPUT_FLOOR'")


try:
    reliability.sweep = _boom
    out = mcp_server._tool_health({"run": True})
    # Assert the DEGRADE CONTRACT structurally (not by matching the message words): a dict, with a populated string
    # `error` field (the degraded branch ran instead of raising), and the empty/degraded summary shape.
    ck("_tool_health returns a dict (no raise) when sweep errors", isinstance(out, dict))
    ck("the degraded branch populated a string 'error' field", isinstance(out.get("error"), str) and bool(out.get("error")))
    ck("the surface is not blanked — a structured degraded summary is returned",
       isinstance(out.get("summary"), dict) and out["summary"].get("lanes_total") == 0 and out["summary"].get("metered_total") == 0)
    ck("lanes/metered are present and empty on the degraded path", out.get("lanes") == {} and out.get("metered") == {})
finally:
    reliability.sweep = _orig_sweep

print(f"\n{'OK' if not ck.fails else 'FAIL'} test_health_surface_degrades: {len(ck.fails)} failure(s)")
sys.exit(1 if ck.fails else 0)

"""require() PASSES in a no-provider interpreter; REFUSES only when a gated SDK is present-but-unpatched.

WHY THIS GUARD EXISTS. gate.require() asserted `_any_patched()` unconditionally, so a box with NO provider
SDK installed (a GitHub CI runner, an offline suite) — whose only tie to spendguard is a module doing
`import spendguard; spendguard.require()` at import — got a false SpendGateRefused ("no OpenAI/Anthropic SDK
was patched"). That forced callers either to install SDKs they never call or to weaken the gate, both worse
than the truth: if no provider SDK is importable, nothing can spend, so there is nothing to gate. The REAL
bypass require() exists to catch is a gated SDK that IS importable yet was not patched (wrong python/venv).

This pins the distinction so gate.require() and __init__._auto_install's strict probe cannot drift apart
again. A regex/keyword could not make this call — it is the structural fact "is a gated SDK importable here".
"""
import os
import sys
import tempfile

# Isolated HOME, re-exec'd before spendguard is imported (no ledger pollution; same pattern as the suite's
# other gate tests). This test writes nothing — it only exercises require()'s enforcement branch.
if not os.environ.get("SPENDGUARD_TEST_ISOLATED"):
    os.environ["SPENDGUARD_TEST_ISOLATED"] = "1"
    os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="spendguard-require-")
    os.execv(sys.executable, [sys.executable] + sys.argv)

from spendguard import gate                                   # noqa: E402
from spendguard.gate import SpendGateRefused                  # noqa: E402

failures = 0


def check(label, ok, extra=""):
    global failures
    if not ok:
        failures += 1
    print(f"  [{'OK' if ok else 'FAIL'}] {label}" + (f"  — {extra}" if not ok and extra else ""))


_orig_any = gate._any_patched
_orig_mods = gate.gated_sdk_modules


def test_no_provider_sdk_importable_passes():
    """The CI/offline case: nothing patched AND no gated SDK importable → require() must NOT raise."""
    gate._any_patched = lambda: False
    gate.gated_sdk_modules = lambda: ["a_module_that_is_not_installed_zzz"]   # find_spec -> None -> absent
    try:
        gate.require()
        check("no gated SDK importable → require() passes (no false refusal)", True)
    except SpendGateRefused as e:
        check("no gated SDK importable → require() passes (no false refusal)", False, str(e)[:90])
    finally:
        gate._any_patched, gate.gated_sdk_modules = _orig_any, _orig_mods


def test_present_but_unpatched_refuses():
    """The real bypass: a gated SDK IS importable but nothing is patched → require() must refuse loudly."""
    gate._any_patched = lambda: False
    gate.gated_sdk_modules = lambda: ["json"]                 # always importable → simulates present-but-unpatched
    try:
        gate.require()
        check("gated SDK present but unpatched → require() REFUSES", False, "did not raise")
    except SpendGateRefused:
        check("gated SDK present but unpatched → require() REFUSES", True)
    finally:
        gate._any_patched, gate.gated_sdk_modules = _orig_any, _orig_mods


def test_disabled_still_refuses():
    """A disabled gate is never a pass, regardless of SDK presence."""
    os.environ["GATE_DISABLE"] = "1"
    try:
        gate.require()
        check("GATE_DISABLE → require() REFUSES", False, "did not raise")
    except SpendGateRefused:
        check("GATE_DISABLE → require() REFUSES", True)
    finally:
        os.environ.pop("GATE_DISABLE", None)


for _fn in (test_no_provider_sdk_importable_passes,
            test_present_but_unpatched_refuses,
            test_disabled_still_refuses):
    _fn()

print(f"\n{'[FAIL]' if failures else 'OK'} test_require_no_provider: {failures} failure(s)")
sys.exit(1 if failures else 0)

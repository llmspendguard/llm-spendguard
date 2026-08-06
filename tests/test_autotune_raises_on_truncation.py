"""Autotune must act in BOTH directions — and the direction that matters is UP.

WHY THIS GUARD EXISTS. `_autotune` shrank wasteful caps and refused to touch any class with truncation
history. Shrinking saves nothing: billing is on tokens GENERATED, so a cap never reached costs zero. The
direction that loses money is a cap set too LOW — the model is cut off, you are billed for the input plus a
partial body, and an incomplete JSON object reads as "no findings" rather than "no answer". Measured on the
real ledger before this fix: 978 calls ended at max_tokens and 187 at length, $23.57, 8.3% of realtime spend,
with claude-sonnet-4-6 truncating 19% of the time.

The veto made it worse than inert: `truncations > 0 -> return` meant it stood down on exactly the classes
that were bleeding, and acted only where there was nothing to save. Same defect as a leak check that watches
one direction.
"""
import os, sys, tempfile

# ISOLATED HOME, re-exec'd before spendguard is imported. This test SEEDS observations, and seeding a
# synthetic truncating class into the real gate_calls would make the shipped estimator predict production
# work from a fixture — the same pollution the calibration harness had to be namespaced to avoid.
if not os.environ.get("SPENDGUARD_TEST_ISOLATED"):
    os.environ["SPENDGUARD_TEST_ISOLATED"] = "1"
    os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="spendguard-autotune-")
    os.execv(sys.executable, [sys.executable] + sys.argv)

import contextlib, io                                    # noqa: E402

from spendguard import bulkgate, gate                     # noqa: E402

failures = 0


def check(label, ok, extra=""):
    global failures
    if not ok:
        failures += 1
    print(f"  [{'OK' if ok else 'FAIL'}] {label}" + (f"  — {extra}" if not ok and extra else ""))


def _seed(sig, model, sizes, truncated, cap):
    for n in sizes:
        bulkgate.note_response(sig, model, n, cap, "max_tokens" if truncated else "end_turn")


def test_a_truncating_class_gets_its_cap_RAISED():
    os.environ["SPENDGUARD_AUTOTUNE"] = "apply"
    model, cap = "claude-opus-4-8", 500
    sig = bulkgate.sig(model, template_id=None)
    # A class that keeps hitting a 500 cap. Truncated samples measure the CAP, not the work, so they are
    # censored from the percentiles — but they are the evidence the cap is wrong, so they must still count.
    _seed(sig, model, [900, 1100, 1000, 950, 1050] * 4, truncated=False, cap=4000)
    _seed(sig, model, [500] * 10, truncated=True, cap=cap)
    kw = {"max_tokens": cap}
    with contextlib.redirect_stderr(io.StringIO()) as err:
        gate._autotune(kw, model)
    check("a truncating class gets its cap RAISED", kw["max_tokens"] > cap,
          f"kept {kw['max_tokens']} — the money-losing direction is the one autotune exists to fix")
    check("...and the REASON is stated, not just the change", "TRUNCATED" in err.getvalue())


def test_a_raise_never_exceeds_the_models_published_ceiling():
    """A cap above what the provider will emit is not a bigger answer, it is a request the API may reject."""
    from spendguard import pricing
    os.environ["SPENDGUARD_AUTOTUNE"] = "apply"
    model = "claude-opus-4-8"
    ceiling = pricing.max_output_tokens(model)
    if not ceiling:
        # No published ceiling here (an isolated HOME has no synced limits cache). That is the honest
        # "no opinion" case, and the production rule is the same: never invent a bound. Assert the raise
        # still happens rather than asserting against a number nobody published.
        sig = bulkgate.sig(model, template_id="no-ceiling-probe")
        _seed(sig, model, [900] * 30, truncated=False, cap=4000)
        _seed(sig, model, [100] * 10, truncated=True, cap=100)
        kw = {"max_tokens": 100}
        with contextlib.redirect_stderr(io.StringIO()):
            gate._autotune(kw, model)
        check("with no published ceiling a raise still happens, bounded only by measurement",
              kw["max_tokens"] > 100, str(kw["max_tokens"]))
        return
    sig = bulkgate.sig(model, template_id="ceiling-probe")
    _seed(sig, model, [ceiling] * 40, truncated=True, cap=ceiling)
    kw = {"max_tokens": 100}
    with contextlib.redirect_stderr(io.StringIO()):
        gate._autotune(kw, model)
    check("a raise never exceeds the published output ceiling", kw["max_tokens"] <= ceiling,
          str(kw["max_tokens"]))


def test_shrink_still_refuses_a_class_that_has_ever_truncated():
    """Clamping a class that has been cut off before would re-create the failure it just recovered from."""
    os.environ["SPENDGUARD_AUTOTUNE"] = "apply"
    model = "claude-haiku-4-5"
    sig = bulkgate.sig(model, template_id="shrink-veto-probe")
    _seed(sig, model, [50] * 40, truncated=False, cap=100000)
    _seed(sig, model, [100000], truncated=True, cap=100000)
    kw = {"max_tokens": 100000}
    with contextlib.redirect_stderr(io.StringIO()):
        gate._autotune(kw, model)
    check("a class that has truncated is never clamped DOWN", kw["max_tokens"] == 100000,
          str(kw["max_tokens"]))


def test_autotune_never_invents_a_cap_where_the_caller_set_none():
    """Omitting max_tokens is a deliberate choice — the caller wants the model's own ceiling."""
    os.environ["SPENDGUARD_AUTOTUNE"] = "apply"
    kw = {}
    with contextlib.redirect_stderr(io.StringIO()):
        gate._autotune(kw, "claude-opus-4-8")
    check("autotune never invents a cap where the caller set none", "max_tokens" not in kw)


for _fn in (test_a_truncating_class_gets_its_cap_RAISED,
            test_a_raise_never_exceeds_the_models_published_ceiling,
            test_shrink_still_refuses_a_class_that_has_ever_truncated,
            test_autotune_never_invents_a_cap_where_the_caller_set_none):
    _fn()

print(f"\n{'[FAIL]' if failures else 'OK'} test_autotune_raises_on_truncation: {failures} failure(s)")
sys.exit(1 if failures else 0)

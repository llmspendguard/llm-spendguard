"""A reasoning model's learned max_tokens may NEVER collapse below the reasoning-inclusive floor.

WHY THIS GUARD EXISTS. gate_calls.out_tok records only the VISIBLE answer, not the hidden reasoning tokens a
reasoning model ALSO bills as output. So a reasoning class whose replies truncate IN REASONING lands tiny
"complete" visible outputs, dragging p99 to ~5 and recommend (p99*1.5) to ~7 — which `_autotune` (apply) then
SHRINKS the real budget down to, and every subsequent reasoning reply truncates, recorded tiny again: a
self-reinforcing spiral. Measured on the real ledger 2026-10-12: honestreview judges clamped to a 7-token cap,
honestreview:coding_router truncated 3,337/10,515 (31.7%). See docs/GUARDRAILS_reasoning_overspend.md.

THE FIX under test: bulkgate.maxtokens floors `recommend` at REASONING_OUT_ESTIMATE whenever the CALL's model
reasons_by_default, keyed off the model actually running (not the stored rows, which may be from a different,
non-reasoning model — e.g. a judge just repinned gemini→gpt-5.5). A visible-answer p99 can then never starve a
reasoning reply, and _autotune can never shrink a reasoning budget below what reasoning needs.
"""
import os, sys, tempfile

# ISOLATED HOME, re-exec'd before spendguard is imported — seeding synthetic classes into the real gate_calls
# would make the shipped estimator predict production work from a fixture (the calibration-pollution trap).
if not os.environ.get("SPENDGUARD_TEST_ISOLATED"):
    os.environ["SPENDGUARD_TEST_ISOLATED"] = "1"
    os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="spendguard-rfloor-")
    os.execv(sys.executable, [sys.executable] + sys.argv)

import contextlib, io                                      # noqa: E402

from spendguard import bulkgate, gate, models               # noqa: E402

failures = 0
FLOOR = bulkgate._reasoning_out_estimate()


def check(label, ok, extra=""):
    global failures
    if not ok:
        failures += 1
    print(f"  [{'OK' if ok else 'FAIL'}] {label}" + (f"  — {extra}" if not ok and extra else ""))


@contextlib.contextmanager
def reasons(is_reasoning):
    """Pin models.reasons_by_default so the test exercises maxtokens' FLOOR LOGIC, not a model's catalog metadata
    (which an isolated HOME may not have synced) — the unit under test is the floor, keyed off that boolean."""
    orig = models.reasons_by_default
    models.reasons_by_default = lambda _m: is_reasoning
    try:
        yield
    finally:
        models.reasons_by_default = orig


def _seed_tiny_complete(sig, model, n=40):
    # THE POISON: many SMALL, non-truncated visible outputs. A reasoning reply cut off in reasoning records a tiny
    # visible out_tok and (on the broken recording path) finish != length, so it reads as a 'complete' 5-token
    # output. p99 of such a class is ~5 and recommend (p99*1.5) ~7 — the value that then clamps the real budget.
    for _ in range(n):
        bulkgate.note_response(sig, model, 5, 4000, "end_turn")


def test_reasoning_model_recommend_is_floored_despite_tiny_poisoned_history():
    model = "test:reasoner"
    sig = bulkgate.sig(model, template_id="rfloor-poison")
    _seed_tiny_complete(sig, model)
    with reasons(True):
        rec = (bulkgate.maxtokens(sig, model=model) or {}).get("recommend")
    check("reasoning model recommend floored at the reasoning estimate despite p99=5",
          rec is not None and rec >= FLOOR, f"got {rec}, floor {FLOOR}")


def test_non_reasoning_model_is_not_floored():
    model = "test:plain"
    sig = bulkgate.sig(model, template_id="rfloor-plain")
    _seed_tiny_complete(sig, model)
    with reasons(False):
        rec = (bulkgate.maxtokens(sig, model=model) or {}).get("recommend")
    check("non-reasoning model keeps its small measured recommend (the floor is reasoning-specific, not blanket)",
          rec is not None and rec < FLOOR, f"got {rec}, floor {FLOOR}")


def test_no_model_arg_does_not_floor():
    """Back-compat: a caller that does not pass `model` is unchanged — the floor needs to know what runs."""
    model = "test:reasoner"
    sig = bulkgate.sig(model, template_id="rfloor-nomodel")
    _seed_tiny_complete(sig, model)
    with reasons(True):                                    # even if the model WOULD reason, no model arg → no floor
        rec = (bulkgate.maxtokens(sig) or {}).get("recommend")
    check("without a model argument the recommend is the raw measured value (no floor applied)",
          rec is not None and rec < FLOOR, f"got {rec}, floor {FLOOR}")


def test_autotune_never_shrinks_a_reasoning_models_budget_below_the_floor():
    """The exact honestreview-judge spiral: a safe large budget, a class showing 0 truncations and p99=5, and
    autotune in apply mode. Before the fix it clamped to ~7; it must never go below the reasoning floor."""
    os.environ["SPENDGUARD_AUTOTUNE"] = "apply"
    model = "test:reasoner-autotune"
    sig = bulkgate.sig(model, template_id=None)            # _autotune builds this sig (no intent context in-test)
    _seed_tiny_complete(sig, model)
    kw = {"max_tokens": 32000}                             # a safe budget expect() would have resolved
    with reasons(True), contextlib.redirect_stderr(io.StringIO()):
        gate._autotune(kw, model)
    check("autotune does not shrink a reasoning model's budget below the reasoning floor",
          kw["max_tokens"] >= FLOOR, f"clamped to {kw['max_tokens']}, floor {FLOOR}")


for _fn in (test_reasoning_model_recommend_is_floored_despite_tiny_poisoned_history,
            test_non_reasoning_model_is_not_floored,
            test_no_model_arg_does_not_floor,
            test_autotune_never_shrinks_a_reasoning_models_budget_below_the_floor):
    _fn()

print(f"\n{'[FAIL]' if failures else 'OK'} test_maxtokens_reasoning_floor: {failures} failure(s)")
sys.exit(1 if failures else 0)

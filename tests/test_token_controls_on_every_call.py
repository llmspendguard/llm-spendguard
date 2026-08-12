"""Both token controls run on EVERY call, not on the ones whose author remembered them.

(Named short on purpose: a test file whose stem is  plus exactly 35 characters matches the shape of a
Lob API key, and CI's secret scanner flags it. See test_ci_pins.py — this file's first name did exactly that.)

WHY. Truncation produced wrong answers here repeatedly, and every time the machinery to stop it already
existed and was not on the path:
  · bulkgate.is_truncated — "a fact, not a guess" — had no caller outside its own module
  · bulkgate.maxtokens — learns the p99 output length per call-class — no judging script used it
  · adapters' `finish_reason` — DECLARED in the result dict, documented as how callers detect a cut reply,
    and assigned by no provider branch, so it was None on every response ever returned
  · vendor_call.input_limit — one caller, and it was a probe script

The first fix put the logic in a SIBLING function (`call_complete`) that callers opted into. Within the hour
1 of 9 judging scripts used it, and a name-review re-run reported 40 of 79 groups "UNREVIEWED" that were
really just truncated. Opt-in safety is absent exactly when it matters. So both controls now live inside
`adapters.call` — the one function every LLM call in this package and every probe already goes through —
and this file fails if either of them can be bypassed.
"""
import inspect
import os
import sys
import tempfile

os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp()      # never touch the real one
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

failures = []


def check(label, ok, extra=""):
    print(f"  [{'OK' if ok else 'FAIL'}] {label}" + (f"  {extra}" if extra and not ok else ""))
    if not ok:
        failures.append(label)


from spendguard import adapters                                                        # noqa: E402

print("-- the guards are on the DEFAULT path, not an opt-in sibling --")
src = inspect.getsource(adapters.call)
check("adapters.call routes through the guard by default", "_call_guarded" in src)
check("...and only skips it when explicitly told to", "_no_guard" in src)
check("call_complete is the SAME object, not a second way to call",
      adapters.call_complete is adapters.call)

guard = inspect.getsource(adapters._call_guarded)
check("the OUTPUT guard checks the provider's own truncation field", "is_truncated" in guard)
check("...retries with a larger budget rather than returning a cut body", "budget * 2" in guard)
check("...and returns text=None when it still will not fit", '"text": None' in guard)
check("...and sizes from the measured p99 when a sig is given", "maxtokens(" in guard)
check("the INPUT guard runs before anything is sent", guard.index("_input_fits") < guard.index("while True"))

print("\n-- truncation cannot be read as a short answer (offline, no network) --")
_calls = {"n": 0}


def _fake_once(model, prompt, max_tokens=512, **kw):
    """A provider that always hits the cap — the shape that produced every silent wrong answer."""
    _calls["n"] += 1
    return {"provider": "fake", "model": model, "text": "{\"partial\": tru", "in_tok": 10,
            "out_tok": max_tokens, "latency": 0.0, "cost": 0.0, "finish_reason": "length", "error": None}


_orig_once, _orig_fits = adapters._call_once, adapters._input_fits
adapters._call_once = _fake_once
adapters._input_fits = lambda *a, **k: (True, "stubbed")
try:
    r = adapters.call("fake-model", "anything", max_tokens=16, retries=2)
    check("a persistently truncated reply is flagged", r.get("truncated") is True)
    check("...its cut body is NOT returned", r.get("text") is None)
    check("...and it carries an error rather than looking successful", bool(r.get("error")))
    check("...after actually retrying with a bigger budget", _calls["n"] == 3, f"made {_calls['n']} call(s)")
    check("...the budget really doubled", r.get("max_tokens_used") == 64, str(r.get("max_tokens_used")))

    # A reply that FITS must pass through untouched — a guard that mangles good answers is worse than none.
    def _fake_ok(model, prompt, max_tokens=512, **kw):
        return {"provider": "fake", "model": model, "text": "ok", "in_tok": 10, "out_tok": 2,
                "latency": 0.0, "cost": 0.0, "finish_reason": "stop", "error": None}
    adapters._call_once = _fake_ok
    r2 = adapters.call("fake-model", "anything", max_tokens=16)
    check("a complete reply passes through", r2.get("text") == "ok" and r2.get("truncated") is False)
finally:
    adapters._call_once, adapters._input_fits = _orig_once, _orig_fits

print("\n-- the input guard blocks BEFORE the request, and reads the right unit --")
from spendguard import vendor_call                                                     # noqa: E402
vendor_call.record_input_limit("fake", "fake-model", 20, "test", source="guard")
ok, why = adapters._input_fits("fake:fake-model", "x" * 500, None)
check("an oversized payload is refused", ok is False, why)
check("...measured in CHARS, the unit record_input_limit stores", "chars" in why)
ok2, why2 = adapters._input_fits("fake:fake-model", "hi", None)
check("a payload within the limit passes", ok2 is True, why2)
ok3, why3 = adapters._input_fits("fake:no-limit-recorded", "x" * 5000, None)
check("an UNMEASURED model passes (unmeasured is not unlimited, but it is not a reason to block)", ok3 is True)
check("...and says so rather than implying it was checked", "UNMEASURED" in why3)

# THE BUG THIS TEST WAS WRITTEN AFTER: input_limit returns the whole RECORD, `int(record)` raised TypeError,
# and the except reported "check unavailable" and PASSED — the guard silently checked nothing.
fits = inspect.getsource(adapters._input_fits)
check("the record's max_chars is read, not the record itself", 'rec.get("max_chars")' in fits)
check("a failing input check WARNS instead of passing silently", "warn_once" in fits)

print(f"\n{'[FAIL]' if failures else 'OK'} test_token_controls_on_every_call: {len(failures)} failure(s)")
sys.exit(1 if failures else 0)

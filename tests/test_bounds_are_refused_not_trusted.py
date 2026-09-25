"""Caller-supplied bounds: the OUTPUT bound is IGNORED (spendguard owns it), the INPUT bound is REFUSED when impossible.

WHY. The same mistake recurred all day and it was never a knowledge problem — every call site is a fresh chance to type
a number, and a wrong one does not announce itself (max_tokens=2000 on kimi-k3 → HTTP 200 with ZERO characters on 19/20
calls; a 600-cap probe written HOURS after that lesson). The first fix VALIDATED the caller's output cap against
measurement and refused a low one. The doctrine went further (docs/CANONICAL_CONCERNS.json: output_budget): a caller's
output max_tokens can only ever set the budget too LOW and truncate, and a high one is free (billing is on tokens
GENERATED) — so spendguard simply IGNORES it and sends the model CEILING. A caller value is no longer validated or
refused; it is not consulted. The way to never truncate: pass nothing — spendguard already does the right thing.

The INPUT axis is different: an input over the model's context WINDOW cannot be sent at all, so it is REFUSED here
(BadBound), before anything is paid for — never silently clipped. Output ignored, input refused; the two axes stay independent.
"""
import os, sys, tempfile

if not os.environ.get("SPENDGUARD_TEST_ISOLATED"):
    os.environ["SPENDGUARD_TEST_ISOLATED"] = "1"
    os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="spendguard-bounds-")
    os.execv(sys.executable, [sys.executable] + sys.argv)

from spendguard import bulkgate, pricing, vendor_call as vc     # noqa: E402

failures = 0


def check(label, ok, extra=""):
    global failures
    if not ok:
        failures += 1
    print(f"  [{'OK' if ok else 'FAIL'}] {label}" + (f"  — {extra}" if not ok and extra else ""))


MODEL, PURPOSE = "claude-opus-4-8", "bounds-probe"
sig = bulkgate.sig(MODEL, template_id=PURPOSE)
for _ in range(30):
    bulkgate.note_response(sig, MODEL, 4000, 64000, "end_turn")

sent = []
_orig = vc._attempt
try:
    vc._attempt = lambda *a, **k: sent.append(1) or {"text": "hi", "out_tok": 5, "finish_reason": "end_turn"}

    # OUTPUT SIDE: a caller's max_tokens is IGNORED (docs/CANONICAL_CONCERNS.json: output_budget), not validated/refused.
    # spendguard sends the model CEILING regardless — a caller value can only ever set it too LOW and truncate, so it is
    # simply not consulted. A low output cap therefore no longer RAISES; the call proceeds and the ceiling protects the answer.
    sent.clear()
    r = vc.call("anthropic", MODEL, "hi", deadline_s=30, purpose=PURPOSE, max_tokens=600)   # a low cap — ignored, not refused
    check("a low caller output cap is IGNORED (not refused) — the call proceeds on the ceiling budget", r.ok and bool(sent))

    sent.clear()
    r = vc.call("anthropic", MODEL, "hi", deadline_s=30, purpose=PURPOSE, max_tokens=64000)   # a high cap — likewise ignored
    check("a high caller output cap is likewise ignored — the call proceeds", r.ok and bool(sent))

    sent.clear()
    r = vc.call("anthropic", MODEL, "hi", deadline_s=30, purpose=PURPOSE)
    check("omitting the cap entirely works — the measured bound is used", r.ok and bool(sent))

    # INPUT side: the provider would reject it anyway; failing here names WHICH field was too big.
    sent.clear()
    win = pricing.max_input_tokens(MODEL)
    if win:
        try:
            vc.call("anthropic", MODEL, "x" * (int(win) * 4 + 100_000), deadline_s=30, purpose=PURPOSE)
            check("input over the context window is REFUSED", False, "the call went through")
        except vc.BadBound as e:
            check("input over the context window is REFUSED", True)
            check("...and it refuses to trim the prompt silently, which would change the task",
                  "never trim" in str(e))
        check("nothing was SENT for an over-window request", not sent)
    else:
        # Absence is unknown, never a verdict: with no published window there is no bound to enforce.
        check("no published window -> no invented bound (call proceeds)",
              vc.call("anthropic", MODEL, "x" * 400, deadline_s=30, purpose=PURPOSE).ok)
finally:
    vc._attempt = _orig

# ── the lookup key must match the recording key, or "measured" silently means "guessed" ──────────────
# (This alignment matters for the ESTIMATE now, not the budget: the send budget is the model ceiling
# (adapters.output_budget, docs/CANONICAL_CONCERNS.json) — vendor_call no longer sizes a cap from the observed
# recommend, so output_cap was removed. class_sig must still match bulkgate's recording key or the estimate misses.)
probe_sig = vc.class_sig(MODEL, PURPOSE)
check("class_sig() is what recording uses, so an ESTIMATE lookup cannot miss it",
      probe_sig == bulkgate.sig(MODEL, template_id=PURPOSE))

# ── the DEADLINE is a bound too, and it went unguarded while max_tokens was guarded ──────────────────
# The asymmetry cost a whole experiment: a probe passed deadline_s=150 against a class whose calls really
# take 56-116s, and most results came back `deadline_exceeded` — which reads as a vendor failure and is a
# caller mistake. Below-measurement deadlines are self-inflicted, deterministic, and paid for: the input
# bills whether or not you stay to hear the answer.
dsig = vc.class_sig(MODEL, "deadline-probe")
for _ in range(12):
    bulkgate.note_latency(dsig, MODEL, 90.0)
p95 = bulkgate.latency(sig=dsig, model=MODEL).get("p95")
check("latency exposes p95 — the quantile a deadline is actually sized from", p95 == 90.0, str(p95))

sent.clear()
try:
    vc.call("anthropic", MODEL, "hi", deadline_s=50, purpose="deadline-probe")
    check("a deadline below the measured p95 is REFUSED", False, "the call went through")
except vc.BadBound as e:
    check("a deadline below the measured p95 is REFUSED", True)
    check("...and it names the measured number and the PUBLIC remedy (adapters.deadline_for, not the internal)",
          "90s" in str(e) and "deadline_for" in str(e), str(e)[:100])
check("nothing was SENT for an under-budgeted call", not sent)

for _ in range(30):                       # the sig also needs an output-cap measurement, or call() stops
    bulkgate.note_response(dsig, MODEL, 4000, 64000, "end_turn")   # earlier for lack of a cap, not a deadline
_orig2 = vc._attempt
vc._attempt = lambda *a, **k: sent.append(1) or {"text": "hi", "out_tok": 5, "finish_reason": "end_turn"}
try:
    r = vc.call("anthropic", MODEL, "hi", deadline_s=400, purpose="deadline-probe")
    check("a generous deadline is accepted", r.ok, r.error or r.kind)
finally:
    vc._attempt = _orig2

# A single observation is an anecdote, not a distribution: a validator that refuses on n=1 blocks real work
# while citing a "measurement". This is the failure that broke test_vendor_call's deliberate 1s deadline.
thin = vc.class_sig(MODEL, "thin-evidence-probe")
bulkgate.note_latency(thin, MODEL, 90.0)
try:
    vc._attempt = lambda *a, **k: {"text": "hi", "out_tok": 5, "finish_reason": "end_turn"}
    vc.call("anthropic", MODEL, "hi", deadline_s=1.0, purpose="thin-evidence-probe", max_tokens=100)
    check(f"a bound is NOT refused on fewer than {vc.MIN_BOUND_OBS} observations", True)
except vc.BadBound as e:
    check(f"a bound is NOT refused on fewer than {vc.MIN_BOUND_OBS} observations", False, str(e)[:110])
finally:
    vc._attempt = _orig2

print(f"\n{'[FAIL]' if failures else 'OK'} test_bounds_are_refused_not_trusted: {failures} failure(s)")
sys.exit(1 if failures else 0)

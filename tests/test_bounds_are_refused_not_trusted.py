"""A caller-supplied bound is VALIDATED against measurement, never trusted. Both directions: output and input.

WHY THIS GUARD EXISTS. The same mistake recurred all day and it was never a knowledge problem — every call
site is a fresh chance to type a number, and a wrong one does not announce itself:

    max_tokens=2000 on kimi-k3   -> HTTP 200 with ZERO characters on 19 of 20 calls (reasoning ate the budget)
    max_tokens=600  in a probe   -> written HOURS after that lesson, by someone who knew it
    deadline_s=180  for 4 vendors -> measured p99s were 49s and 446s; the slow ones died by construction

Advice in a docstring loses to a literal at the call site, every time. `call()` had a hole exactly the shape
of that mistake: `max_tokens is None` took the measured path, and any other value went straight through
unchecked. Now a bound below what the class demonstrably produces is refused BEFORE anything is sent — the
one place where refusing is free, because nothing has been paid for yet.

The way to never see BadBound: do not pass a bound. Omit it and the measured one is used.
"""
import os, sys, tempfile

if not os.environ.get("SPENDGUARD_TEST_ISOLATED"):
    os.environ["SPENDGUARD_TEST_ISOLATED"] = "1"
    os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="spendguard-bounds-")
    os.execv(sys.executable, [sys.executable] + sys.argv)

from spendguard import adapters, bulkgate, pricing, vendor_call as vc     # noqa: E402

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

    try:
        vc.call("anthropic", MODEL, "hi", deadline_s=30, purpose=PURPOSE, max_tokens=600)
        check("a cap below the measured p95 is REFUSED", False, "the call went through")
    except vc.BadBound as e:
        check("a cap below the measured p95 is REFUSED", True)
        check("...and the message carries the MEASURED number, not just a complaint", "4,000" in str(e), str(e)[:90])
        check("...and it says a cap was never a cost control", "GENERATED" in str(e))
    check("nothing was SENT — refusing costs nothing only if it happens first", not sent)

    sent.clear()
    r = vc.call("anthropic", MODEL, "hi", deadline_s=30, purpose=PURPOSE, max_tokens=64000)
    check("a GENEROUS caller cap is accepted", r.ok and bool(sent))

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
probe_sig = vc.class_sig(MODEL, PURPOSE)
check("class_sig() is what recording uses, so a lookup cannot miss it",
      probe_sig == bulkgate.sig(MODEL, template_id=PURPOSE))
cap, basis = vc.output_cap("anthropic", MODEL, sig=probe_sig)
check("output_cap's OBSERVED rung actually fires (it never did: raw purpose vs hashed sig)",
      basis in ("observed", "registry:probe") and cap, f"{cap} {basis}")
check("...and the observed cap is the measured recommendation FLOORED at 32K, never a ceiling",
      cap == max(int(bulkgate.maxtokens(probe_sig)["recommend"]), adapters.TOKEN_FLOOR), str(cap))

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
    check("...and it names the measured number and the remedy",
          "90s" in str(e) and "time_budget" in str(e), str(e)[:100])
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

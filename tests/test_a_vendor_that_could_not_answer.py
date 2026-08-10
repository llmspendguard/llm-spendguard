"""A vendor that COULD NOT answer is not a vendor that found nothing — and each way of failing is different.

TWO FAILURES FROM ONE REVIEW WAVE, both of which read as "3 of 4 vendors reported" and neither of which was
what happened.

  z.ai returned HTTP 429 on all 10 files:
      {'error': {'code': '1113', 'message': 'Insufficient balance or no resource package. Please recharge.'}}
  That is not rate limiting. It is permanent until a person acts, it is fixable in thirty seconds, and it
  was reported in the same word as a network blip — `transport_error` — so the panel retried it ten times,
  paid the latency, and produced a run that looked like it had four reviewers while it had three. The
  findings gate then counted "2 vendors agreed" out of a denominator that was quietly wrong.

  kimi-k3 was killed by its deadline on conv.py (37,453 chars compacted). Not a refusal and not an input
  limit: given room, the same call SUCCEEDS in 547 seconds and returns 17,980 output tokens. The budget was
  drawn from that model's whole review population, where p50 is 38s and p95 is 289s — and that spread IS
  the payload-size effect. One number for both ends is too generous for the small files and fatal for the
  large ones, which is the same lesson max_tokens taught, one dimension deeper.

WHAT THIS FILE PINS
  1. an account that cannot pay is UNFUNDED, and is not retried
  2. a real rate-limit 429 is still transport, and IS retried — the distinction has to cut both ways
  3. latency observations carry their payload size
  4. a budget prefers observations of a COMPARABLE payload, and says which population it used
  5. ...but only when it has enough of them; a thin band is noise wearing a narrower label
"""
import os
import sys
import tempfile

if not os.environ.get("SPENDGUARD_TEST_ISOLATED"):
    os.environ["SPENDGUARD_TEST_ISOLATED"] = "1"
    os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="spendguard-couldnot-")
    os.execv(sys.executable, [sys.executable] + sys.argv)

from spendguard import bulkgate, vendor_call as vc                          # noqa: E402

failures = 0


def check(label, cond, extra=""):
    global failures
    if not cond:
        failures += 1
    print(f"  [{'OK' if cond else 'FAIL'}] {label}{('  — ' + extra) if extra and not cond else ''}")


print("  how a vendor failed is recorded, not flattened:")
ZAI_429 = ("Error code: 429 - {'error': {'code': '1113', 'message': "
           "'Insufficient balance or no resource package. Please recharge.'}}")
kind, why = vc._classify({"error": ZAI_429})
check("'insufficient balance' on a 429 is UNFUNDED", kind == vc.UNFUNDED, f"{kind} / {why}")
check("...and says the account cannot pay, not 'transport'", why == "account_cannot_pay", str(why))

# THE DISTINCTION MUST CUT BOTH WAYS. Calling every 429 permanent would suppress the retry that fixes a
# real rate limit — a worse bug than the one being fixed, and invisible in the same way.
kind, _ = vc._classify({"error": "Error code: 429 - Rate limit reached for gpt-5.5 in organization org-x"})
check("a REAL rate-limit 429 is still transport, so it is still retried", kind == vc.TRANSPORT_ERROR, kind)

kind, _ = vc._classify({"error": "Error code: 400 - the request was rejected because it was "
                                 "considered high risk"})
check("a policy rejection is still REFUSED", kind == vc.REFUSED, kind)
kind, _ = vc._classify({"error": "APIConnectionError: Connection reset by peer"})
check("a network fault is still transport", kind == vc.TRANSPORT_ERROR, kind)

# UNFUNDED must be outside the retry set, or the classification changes the label and nothing else.
import inspect                                                              # noqa: E402
_src = inspect.getsource(vc.call)
check("only transport failures are retried, so UNFUNDED breaks immediately",
      "if kind != TRANSPORT_ERROR" in _src and "break" in _src,
      "an unfunded account retried three times is three times nothing, at three times the latency")


print("\n  a deadline is sized from payloads of a COMPARABLE size:")
SIG, MODEL = "sig-under-test", "model-under-test"
# Two populations, an order of magnitude apart in size and in time — the real shape of review calls.
for _ in range(12):
    bulkgate.note_latency(SIG, MODEL, 8.0, in_chars=3_000)
for _ in range(12):
    bulkgate.note_latency(SIG, MODEL, 500.0, in_chars=37_000)

check("the observation carries its payload size",
      bool(bulkgate._calls_db().execute(
          "SELECT COUNT(*) FROM gate_latency WHERE in_chars > 0").fetchone()[0]))

mixed = bulkgate.latency(sig=SIG, model=MODEL)
small = bulkgate.latency(sig=SIG, model=MODEL, near_chars=3_000)
large = bulkgate.latency(sig=SIG, model=MODEL, near_chars=37_000)
check("the mixed population sits between the two", 8.0 <= mixed["p95"] <= 500.0, str(mixed.get("p95")))
check("a small payload gets the small population", small["p95"] <= 10.0,
      f"p95 {small.get('p95')} from {small.get('scope')}")
check("a large payload gets the large one", large["p95"] >= 400.0,
      f"p95 {large.get('p95')} from {large.get('scope')} — the number that killed conv.py")
check("...and each says WHICH population answered",
      small.get("scope") == "~3,000c" and large.get("scope") == "~37,000c",
      f"{small.get('scope')} / {large.get('scope')}")

# A BAND WITH ALMOST NOTHING IN IT IS NOT MORE PRECISE, IT IS JUST NARROWER. Falling back to the whole
# population is the honest answer, and it has to be visible as a fallback.
thin = bulkgate.latency(sig=SIG, model=MODEL, near_chars=1_000_000)
check("a band with too few observations falls back, and says so",
      thin.get("scope") == "all-sizes", str(thin.get("scope")))

print("\n  and the budget asks for the size it is about to send:")
check("time_budget takes the payload size",
      "in_chars" in inspect.signature(vc.time_budget).parameters)
check("note_latency records it", "in_chars" in inspect.signature(bulkgate.note_latency).parameters)
check("latency can be asked for it", "near_chars" in inspect.signature(bulkgate.latency).parameters)

print(f"\n{'[FAIL]' if failures else 'OK'} test_a_vendor_that_could_not_answer: {failures} failure(s)")
sys.exit(1 if failures else 0)

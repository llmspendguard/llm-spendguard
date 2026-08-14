"""The failure taxonomy stays HONEST — the guard for Symptoms A/B/C of the vendor-call transport work.

Each of these was a real way the panel lied about coverage, and each is pinned here so it cannot come back:

  B  a call the join ABANDONED, or a client read-timeout, is DEADLINE_EXCEEDED — the vendor was too slow —
     NOT transport_error. A connection that BROKE (refused/reset) stays transport_error. The two were one
     bucket, so the coverage report could not say whether a vendor was slow or unreachable, and an abandoned
     call was even RETRIED as if a fresh attempt might connect.  Told apart by the exception TYPE adapters
     preserves (error_type) — a structured signal — never by the wording of the message.

  C  a TRUNCATED body is a no-answer, never "no findings". Even after adapters raises the cap and re-asks and
     still gets a cut-off body (so the dict carries an `error` string), the kind is TRUNCATED — and Result.text
     RAISES on it, so a half a JSON object can never be read as an empty/clean review. Measured: a truncated
     kimi review scored as a clean result, one level above the reviewer.

  C-floor  the output cap floors at 32K unless the MODEL's own published max is lower. kimi's stale registry
     26,128 was below what the model finishes a long review in, and output_cap returned it as an explicit cap
     that bypassed the adapters floor. A measured number may RAISE the cap, never lower it below the floor.

Offline, isolated home, zero spend — adapters.call is monkeypatched to return canned transport outcomes.
"""
import os, sys, tempfile

if not os.environ.get("SPENDGUARD_TEST_ISOLATED"):
    os.environ["SPENDGUARD_TEST_ISOLATED"] = "1"
    os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="spendguard-taxonomy-")
    os.execv(sys.executable, [sys.executable] + sys.argv)

from spendguard import vendor_call as vc
from spendguard import adapters, pricing

fails = 0
def ck(label, cond, extra=""):
    global fails
    if not cond:
        fails += 1
    print(f"  [{'OK' if cond else 'FAIL'}] {label}{('  — ' + extra) if extra and not cond else ''}")


print("-- B: a deadline is not a transport fault, told apart by exception TYPE not prose --")
ck("client read-timeout (APITimeoutError) -> deadline_exceeded",
   vc._classify({"error": "x", "error_type": "APITimeoutError"})[0] == vc.DEADLINE_EXCEEDED)
ck("raw httpx ReadTimeout -> deadline_exceeded",
   vc._classify({"error": "x", "error_type": "ReadTimeout"})[0] == vc.DEADLINE_EXCEEDED)
ck("the join ABANDON (deadline flag) -> deadline_exceeded",
   vc._classify({"error": "no response within 30s (abandoned)", "deadline": True})[0] == vc.DEADLINE_EXCEEDED)
ck("a connection that BROKE (APIConnectionError) stays transport_error",
   vc._classify({"error": "connection reset", "error_type": "APIConnectionError"})[0] == vc.TRANSPORT_ERROR)
ck("an error whose PROSE says 'timeout' but is not a timeout type is NOT mislabeled deadline",
   vc._classify({"error": "server said: request timeout budget exceeded upstream", "error_type": "APIError"})[0]
   == vc.TRANSPORT_ERROR)

print("\n-- C: a truncation is a no-answer, even carrying an error, and text is unreadable --")
trunc = vc._classify({"error": "truncated at 256 tokens", "truncated": True, "finish_reason": "length"})
ck("truncated=True (with an error string) -> truncated, not transport_error", trunc[0] == vc.TRUNCATED)
r = vc.Result(vc.TRUNCATED, "moonshot", "kimi-k3", text="{\"findings\": [", stop_reason="length")
ck("a TRUNCATED result is not ok", r.ok is False)
raised = False
try:
    _ = r.text
except vc.NotOk:
    raised = True
ck("...and .text RAISES rather than handing back a half-JSON body", raised)
ck("finish_reason=length with no error still -> truncated",
   vc._classify({"finish_reason": "length", "text": "{"})[0] == vc.TRUNCATED)
ck("an empty 200 (no text) -> empty, never ok", vc._classify({"finish_reason": "stop", "text": ""})[0] == vc.EMPTY)

print("\n-- C-floor: output_cap floors at 32K unless the model's published max is lower --")
vc.record_cap("floortest", "stale-model", 26128, method="probe", source="deliberately below 32K")
cap, basis = vc.output_cap("floortest", "stale-model")
ck("a stale registry cap below 32K is floored UP to 32K", cap == adapters.TOKEN_FLOOR, f"got {cap}")
ck("...and keeps its provenance (basis names the registry, not 'floor')", basis.startswith("registry"), basis)
# a model whose OWN published max is below the floor clamps down to it — the model DID say otherwise
_real_max = pricing.max_output_tokens
pricing.max_output_tokens = lambda m: 8192 if m == "small-window" else _real_max(m)
try:
    cap2, _ = vc.output_cap("floortest", "small-window")
    ck("a model whose published max is 8192 caps at 8192, not 32K (the model said otherwise)", cap2 == 8192,
       f"got {cap2}")
finally:
    pricing.max_output_tokens = _real_max
ck("a model with NO measurement still gets the 32K floor (the floor IS the default, never 'unknown')",
   vc.output_cap("floortest", "never-measured")[0] == adapters.TOKEN_FLOOR)


print("\n-- through fan_out: a truncated vendor is a FAILED vendor; consensus refuses --")
_real_call = adapters.call
def _fake_call(model, prompt, **kw):
    # kimi truncates even after the cap was raised; opus answers cleanly.
    if "kimi" in model:
        return {"provider": "moonshot", "model": "kimi-k3", "text": None, "in_tok": 10, "out_tok": 256,
                "cost": 0.01, "finish_reason": "length", "truncated": True, "error": "truncated at 256 tokens"}
    return {"provider": "anthropic", "model": "claude-opus-4-8", "text": "{\"findings\": []}", "in_tok": 10,
            "out_tok": 4, "cost": 0.02, "finish_reason": "stop", "error": None}
adapters.call = _fake_call
try:
    fan = vc.fan_out([("moonshot", "kimi-k3"), ("anthropic", "claude-opus-4-8")],
                     "review this", deadline_s=60, purpose="review:x")
    kinds = {r.vendor: r.kind for r in fan["results"]}
    ck("the truncated kimi is reported truncated, not ok", kinds.get("moonshot") == vc.TRUNCATED, str(kinds))
    ck("it is in FAILED, not among the answers", any(r.vendor == "moonshot" for r in fan["failed"]))
    ck("the panel is NOT complete (a cut-off vendor did not answer)", fan["complete"] is False)
    refused = False
    try:
        vc.consensus(fan)
    except vc.NotOk:
        refused = True
    ck("consensus() REFUSES a run where a vendor only truncated (no false N-of-N)", refused)
finally:
    adapters.call = _real_call

print(f"\n{'[FAIL]' if fails else 'OK'} test_deadline_truncation_and_floor: {fails} failure(s)")
sys.exit(1 if fails else 0)

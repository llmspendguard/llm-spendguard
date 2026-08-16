"""FULL-STACK INTEGRATION: every vendor x every failure mode yields an HONEST outcome — never a false success.

This is NOT a unit test. It drives the REAL stack end to end — vendor_call.call -> _attempt (on its worker
thread) -> _classify -> Result, and fan_out -> consensus — mocking ONLY the network at adapters.call, the one
thing we cannot exercise offline. Every seam that has burned us is asserted for ALL FIVE panel vendors at once,
so "it works on vendor X" can never again quietly mean "we only tried the happy path on vendor Y":

  transport-vs-deadline · truncation-as-no-findings · empty-as-answer · refusal/unfunded not retried ·
  schema-violation surfaced · and PARTIAL COVERAGE (fan_out is not all-or-none: the vendors that answered are
  usable even when others failed, and `complete`/consensus tell the truth about it).
"""
import os, sys, tempfile

if not os.environ.get("SPENDGUARD_TEST_ISOLATED"):
    os.environ["SPENDGUARD_TEST_ISOLATED"] = "1"
    os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="spendguard-matrix-")
    os.execv(sys.executable, [sys.executable] + sys.argv)

from spendguard import vendor_call as vc, adapters   # noqa: E402

VENDORS = [("anthropic", "claude-opus-4-8"), ("openai", "gpt-5.5"), ("moonshot", "kimi-k3"),
           ("zai", "glm-5.3"), ("gemini", "gemini-2.5-flash")]
SCHEMA = {"type": "object", "properties": {"findings": {"type": "array"}}, "required": ["findings"]}

# Each mode maps to the adapters.call RESULT dict for that outcome and the kind vendor_call must produce.
MODES = {
    "ok":           ({"text": '{"findings": []}', "finish_reason": "stop", "in_tok": 10, "out_tok": 5,
                      "cost": 0.01, "error": None}, vc.OK),
    "empty_200":    ({"text": "", "finish_reason": "stop", "in_tok": 10, "out_tok": 0, "cost": 0.01,
                      "error": None}, vc.EMPTY),
    "truncated":    ({"text": None, "finish_reason": "length", "truncated": True, "in_tok": 10,
                      "out_tok": 256, "cost": 0.02, "error": "truncated at 256 tokens"}, vc.TRUNCATED),
    "read_timeout": ({"text": None, "error": "APITimeoutError: request timed out",
                      "error_type": "APITimeoutError"}, vc.DEADLINE_EXCEEDED),
    "conn_broke":   ({"text": None, "error": "APIConnectionError: connection reset",
                      "error_type": "APIConnectionError"}, vc.TRANSPORT_ERROR),
    "refused":      ({"text": None, "error": "Error code: 400 - content_policy_violation",
                      "finish_reason": None}, vc.REFUSED),
    "unfunded":     ({"text": None, "error": "Error code: 429 - Insufficient balance, please recharge",
                      "finish_reason": None}, vc.UNFUNDED),
    "schema_bad":   ({"text": '{"not_findings": 1}', "finish_reason": "stop", "in_tok": 10, "out_tok": 8,
                      "cost": 0.01, "error": None}, vc.SCHEMA_VIOLATION),
}

fails = 0
def ck(label, cond, extra=""):
    global fails
    if not cond:
        fails += 1
    print(f"  [{'OK' if cond else 'FAIL'}] {label}{('  — ' + extra) if extra and not cond else ''}")


def _mock(result):
    def _call(model, prompt, **kw):
        return dict(result)
    return _call


_real = adapters.call
print("== every vendor x every failure mode -> the honest kind, and never readable as content ==")
for vendor, model in VENDORS:
    print(f"\n-- {vendor}/{model} --")
    for name, (result, expected) in MODES.items():
        schema = SCHEMA if name == "schema_bad" else None
        adapters.call = _mock(result)
        try:
            r = vc.call(vendor, model, "review this", deadline_s=60, max_tokens=32000, schema=schema)
        finally:
            adapters.call = _real
        ck(f"{name:12} -> {expected}", r.kind == expected, f"got {r.kind} (err={r.error!r})")
        if expected != vc.OK:
            readable = True
            try:
                _ = r.text
            except vc.NotOk:
                readable = False
            ck(f"{name:12}    .text refuses — a {expected} can't be read as an answer", not readable)


print("\n== fan_out is NOT all-or-none: partial coverage is usable AND told truthfully ==")
# anthropic + openai answer; moonshot truncates; zai's connection breaks. A real mixed panel.
_MIX = {"anthropic": MODES["ok"][0], "openai": MODES["ok"][0],
        "moonshot": MODES["truncated"][0], "zai": MODES["conn_broke"][0]}
def _mock_mixed(model, prompt, **kw):
    prov = model.split(":", 1)[0]
    return dict(_MIX[prov])
adapters.call = _mock_mixed
try:
    fan = vc.fan_out([("anthropic", "claude-opus-4-8"), ("openai", "gpt-5.5"),
                      ("moonshot", "kimi-k3"), ("zai", "glm-5.3")],
                     "review this", deadline_s=60, purpose="review:x", max_tokens=32000)
finally:
    adapters.call = _real
kinds = {r.vendor: r.kind for r in fan["results"]}
ck("the two that answered ARE usable (partial coverage, not all-or-none)", fan["n_ok"] == 2, str(fan["n_ok"]))
ck("...and the two that failed are labeled honestly (truncated + transport, not 'no findings')",
   kinds.get("moonshot") == vc.TRUNCATED and kinds.get("zai") == vc.TRANSPORT_ERROR, str(kinds))
ck("complete is FALSE — the run does not claim 4/4 when it got 2/4", fan["complete"] is False)
ok_text = sorted(r.text for r in fan["ok"])                       # the answers are readable, the failures are not
ck("the ok answers are readable content", ok_text == ['{"findings": []}', '{"findings": []}'], str(ok_text))
refused = False
try:
    vc.consensus(fan)                                            # asks for all 4 by default -> must refuse
except vc.NotOk:
    refused = True
ck("consensus() refuses a false N-of-N, but consensus(require=2) accepts the partial",
   refused and len(vc.consensus(fan, require=2)) == 2)

print(f"\n{'[FAIL]' if fails else 'OK'} test_failure_matrix_all_vendors: {fails} failure(s)")
sys.exit(1 if fails else 0)

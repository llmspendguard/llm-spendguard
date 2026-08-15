"""honestreview error-capture contract (spendguard_error_capture_prompt.md):

  Ask 1 — a non-ok Result carries FULL failure detail under the EXACT names honestreview logs verbatim:
    error, http_status, provider_error, elapsed_s, attempts, finish_reason, in_tok, out_tok, text_head, run_id.
  Ask 3 — the taxonomy splits overloaded (429/529) from payload_rejected (400/413) from transport/deadline.
  adapters._exc_detail pulls the structured signals (status, body, Retry-After) off a provider SDK exception.

  A failed Result still NEVER exposes `.text` (no false success), and the serialized failure carries no "text".
  (Retry counting itself is verified behaviorally in test_a_vendor_that_could_not_answer.)

Offline, isolated home.
"""
import os
import sys
import tempfile

if not os.environ.get("SPENDGUARD_TEST_ISOLATED"):
    os.environ["SPENDGUARD_TEST_ISOLATED"] = "1"
    os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="spendguard-hrec-")
    os.execv(sys.executable, [sys.executable] + sys.argv)

from spendguard import vendor_call as vc, adapters, crossllm   # noqa: E402

fails = 0


def ck(label, cond, extra=""):
    global fails
    if not cond:
        fails += 1
    print(f"  [{'OK' if cond else 'FAIL'}] {label}{('  — ' + extra) if extra and not cond else ''}")


def _raises(fn):
    try:
        fn()
        return False
    except Exception:
        return True


# ── adapters._exc_detail pulls structured fields off an SDK-style exception ─────────────────────────────────────
class _Resp:
    def __init__(self, status, text, headers):
        self.status_code, self.text, self.headers = status, text, headers


class _SDKErr(Exception):
    def __init__(self):
        super().__init__("overloaded")
        self.status_code = 529
        self.response = _Resp(529, '{"error":{"message":"overloaded, try again"}}', {"retry-after": "3"})
        self.body = {"error": {"message": "overloaded, try again"}}


st, perr, ra = adapters._exc_detail(_SDKErr())
ck("http_status pulled from the exception", st == 529, f"got {st}")
ck("provider_error is the response body (truncated)", bool(perr) and "overloaded" in perr, f"got {perr!r}")
ck("retry_after header captured", ra == "3", f"got {ra!r}")

# ── a non-ok Result exposes honestreview's EXACT field names ────────────────────────────────────────────────────
r = vc.Result(vc.OVERLOADED, "moonshot", "kimi-k3", text="partial body…", stop_reason="length",
              in_tok=100, out_tok=5, latency=1.234, error="429 overloaded",
              http_status=429, provider_error='{"error":"slow down"}', attempts=3)
ck("a failed Result raises on .text (no false success)", _raises(lambda: r.text))
row = r.as_row()
for f in ("error", "http_status", "provider_error", "elapsed_s", "attempts",
          "finish_reason", "in_tok", "out_tok", "text_head", "run_id"):
    ck(f"as_row carries honestreview field {f!r}", f in row, f"missing {f}")
ck("http_status is the code", row.get("http_status") == 429)
ck("attempts is recorded", row.get("attempts") == 3)
ck("elapsed_s == latency seconds", abs((row.get("elapsed_s") or 0) - 1.234) < 1e-3)
ck("finish_reason mirrors stop_reason", row.get("finish_reason") == "length")
ck("text_head carries a peek at the (partial) body", row.get("text_head") == "partial body…")
ck("a failed Result NEVER serializes `text`", "text" not in row)

# ── AskResult.as_dict failure branch carries the full field set, never `text` ───────────────────────────────────
fan = {"run_id": "run-x", "mode": "all", "complete": False, "n": 1, "n_ok": 0,
       "results": [r], "ok": [], "failed": [r]}
res0 = crossllm.AskResult(fan, "all").as_dict()["results"][0]
for f in ("kind", "error", "http_status", "provider_error", "elapsed_s", "attempts",
          "finish_reason", "text_head", "run_id", "in_tok", "out_tok"):
    ck(f"as_dict failure carries {f!r}", f in res0, f"missing {f}")
ck("as_dict failure NEVER carries `text` (no false success on the wire)", "text" not in res0)
ck("the serialized kind is the fine-grained one (overloaded)", res0.get("kind") == "overloaded")

print(f"\n{'[FAIL]' if fails else 'OK'} test_honestreview_error_capture: {fails} failure(s)")
sys.exit(1 if fails else 0)

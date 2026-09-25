"""Guardrail-E (the per-call runaway breaker, bulkgate.check_runaway) covers the SCHEMA path too (Warden #1 part 2).

Warden 2026-09-25: a cheap intent that fails over to an expensive reasoning model runs away (huge out_tok billed as
$). check_runaway must FIRE on such a reply even when the call requested structured output (schema=…) — a schema call
goes through the SAME _call_guarded while-loop, so the breaker sees its out_tok. This proves (a) the breaker is wired
on the schema path (it is CALLED for a schema call), and (b) its arithmetic trips when out_tok >> the class norm.
Offline: _call_once / input-fit / output-ceiling stubbed; no network, no spend."""
import os
import sys
import tempfile

os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="sg-ge-schema-")
os.environ.setdefault("SPENDGUARD_TEST_ISOLATED", "1")
os.environ.setdefault("SPENDGUARD_NO_AUTOINSTALL", "1")
os.environ["SPENDGUARD_ROUTE_THROUGH_QUEUE"] = "0"
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from spendguard import adapters, bulkgate, vendor_call  # noqa: E402

_fails = []
def ck(label, cond):
    if not cond:
        _fails.append(label)
    print(f"  [{'OK' if cond else 'FAIL'}] {label}")


# (1) UNIT: check_runaway trips when out_tok >> a trustworthy class norm (arithmetic on billed tokens).
print("-- (1) check_runaway arithmetic trips on a runaway out_tok --")
norm = {"p99": 200, "n": 50, "recommend": 200}          # WARM class: measured p99=200 over 50 samples
tripped, detail = bulkgate.check_runaway("e2e-sig", "openai:gpt-5.6-sol", 40000, norm=norm)
ck(f"40000 out_tok vs p99=200 trips (detail={detail})", tripped is True)
notrip, _ = bulkgate.check_runaway("e2e-sig", "openai:gpt-5.6-sol", 150, norm=norm)
ck("a normal 150 out_tok does NOT trip", notrip is False)

# (2) WIRING: a SCHEMA call goes through the guarded while-loop, so check_runaway is CALLED with its out_tok.
print("-- (2) a schema= call runs check_runaway on its reply (the breaker covers the schema path) --")
_seen = {}
_real_cr = bulkgate.check_runaway
def _spy_cr(sig, model, out_tok, norm=None):
    _seen["called"] = True
    _seen["out_tok"] = out_tok
    _seen["model"] = model
    return _real_cr(sig, model, out_tok, norm=norm)


def _fake_once(model, prompt, max_tokens=None, **kw):
    # a well-formed STRUCTURED reply with a big (runaway-scale) out_tok
    return {"provider": "openai", "model": model.split(":", 1)[-1], "text": '{"ok": true}', "in_tok": 10,
            "out_tok": 45000, "latency": 0.1, "cost": 0.9, "finish_reason": "stop", "error": None}


bulkgate.check_runaway = _spy_cr
adapters._call_once = _fake_once
adapters._input_fits = lambda *a, **k: (True, "")
adapters._book_substitution = lambda *a, **k: None
vendor_call.served_substitute = lambda v, m: (m, None)
adapters.pricing.output_ceiling = lambda vendor, model, backstop, **kw: 128000
try:
    r = adapters.call("openai:gpt-5.6-sol", "extract per this schema",
                      schema={"type": "object", "properties": {"ok": {"type": "boolean"}}},
                      sig="warden:code_structure")
    ck("check_runaway was CALLED on the schema call (breaker is on the schema path)", _seen.get("called") is True)
    ck("it saw the reply's out_tok (45000)", _seen.get("out_tok") == 45000)
    ck("the schema reply itself still came back (breaker records, never aborts the call)", r.get("text") == '{"ok": true}')
finally:
    bulkgate.check_runaway = _real_cr

print(f"\n{'[FAIL]' if _fails else 'OK'} test_guardrail_e_schema: {len(_fails)} failure(s)")
sys.exit(1 if _fails else 0)

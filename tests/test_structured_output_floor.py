"""A STRUCTURED (schema) reply is JSON a caller parses — so its output budget must have ROOM to close the object,
and a cut one must be a TYPED truncation, never a silent text=None that reads as 'no findings'.

The warden describe-bakeoff bug: a JSON verdict call with a small/explicit cap (a candidate's max_tokens=3000, or a
poisoned low prediction) truncated mid-object; the incomplete JSON parsed to nothing and read as an empty verdict,
silently poisoning the measurement. The 32K TOKEN_FLOOR already protects a NO-CAP call, but an explicit small cap on
a schema call bypassed it. This pins the fix (adapters._call_guarded):
  (a) a schema call is FLOORED to real room (TOKEN_FLOOR, clamped to the model's ceiling) even when the caller passed
      a tiny explicit cap — 64/3000 for a JSON verdict is now impossible;
  (b) a schema call needs no sig/max_tokens (it is safely floored), so it never raises the 'name a budget' ValueError;
  (c) a STILL-truncated structured reply returns text=None + truncated=True + finish_reason='length' + a structured
      error, so vendor_call classifies it TRUNCATED (whose .text RAISES) — 'truncated, retry larger', not 'no answer'.

Hermetic: adapters.call (the _no_guard inner call) is stubbed to capture the budget / force a truncation — no network."""
import os
import sys
import tempfile

os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="spendguard-jsonfloor-")
os.environ.setdefault("SPENDGUARD_TEST_ISOLATED", "1")
os.environ.setdefault("SPENDGUARD_NO_AUTOINSTALL", "1")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from spendguard import adapters, vendor_call   # noqa: E402

fails = []


def ck(name, cond):
    print(("  [OK] " if cond else "  [FAIL] ") + name)
    if not cond:
        fails.append(name)


_SCHEMA = {"type": "object", "required": ["good"], "properties": {"good": {"type": "boolean"}}}
_MODEL = "openai:gpt-5-nano"          # published output ceiling 128000, so the 32K floor applies (32000 < ceiling)
_seen = {}


def _stub_ok(model, prompt, **kw):
    _seen["max_tokens"] = kw.get("max_tokens")
    return {"provider": "openai", "model": "gpt-5-nano", "text": '{"good": true}', "in_tok": 5, "out_tok": 6,
            "cost": 0.0, "finish_reason": "stop", "error": None}


def _stub_truncated(model, prompt, **kw):
    b = int(kw.get("max_tokens") or 0)
    _seen["max_tokens"] = b
    return {"provider": "openai", "model": "gpt-5-nano", "text": '{"good": tr', "in_tok": 5, "out_tok": b,
            "cost": 0.0, "finish_reason": "length", "error": None}      # cut mid-object at the cap


print("-- (a) a schema call with a TINY explicit cap is floored to real room, never the tiny cap --")
adapters.call = _stub_ok
_seen.clear()
adapters._call_guarded(_MODEL, "judge this", schema=_SCHEMA, max_tokens=64, sig="test:verdict", _no_sub=True)
ck("an explicit max_tokens=64 on a JSON call was FLOORED (not sent as 64)", _seen.get("max_tokens") != 64)
ck("...floored to at least TOKEN_FLOOR", (_seen.get("max_tokens") or 0) >= adapters.TOKEN_FLOOR)

print("\n-- (b) a schema call needs no sig / no max_tokens: it is safely floored, not refused --")
_seen.clear()
crashed = False
try:
    adapters._call_guarded(_MODEL, "judge this", schema=_SCHEMA, _no_sub=True)   # no sig, no max_tokens
except ValueError:
    crashed = True
ck("no ValueError for a bare schema call (the floor is the deliberate budget)", not crashed)
ck("...and it still floored to real room", (_seen.get("max_tokens") or 0) >= adapters.TOKEN_FLOOR)

print("\n-- (c) a STILL-truncated structured reply is a TYPED truncation, never a silent empty --")
adapters.call = _stub_truncated
r = adapters._call_guarded(_MODEL, "judge this", schema=_SCHEMA, max_tokens=64, sig="test:verdict",
                           retries=0, _no_sub=True)
ck("text is None (a cut JSON body is never handed over as an answer)", r.get("text") is None)
ck("...typed truncated=True (not a silent short answer)", r.get("truncated") is True)
ck("...finish_reason='length' so it classifies as a truncation", (r.get("finish_reason") or "").lower() == "length")
ck("...the error names it a STRUCTURED truncation (retry larger), not 'no findings'",
   "STRUCTURED" in (r.get("error") or ""))
kind, _stop = vendor_call._classify(r)
ck("vendor_call classifies it TRUNCATED (Result.text then RAISES — can't be read as empty)",
   kind == vendor_call.TRUNCATED)

print(f"\n{'[FAIL]' if fails else 'OK'} test_structured_output_floor: {len(fails)} failure(s)")
sys.exit(1 if fails else 0)

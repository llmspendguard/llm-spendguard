"""Realtime-oracle hardening — un-regressable guards for the honestreview findings on the realtime-reconstruction
feature (all CONFIRMED real against the code):
  #1  Anthropic cache-CREATION tokens are now PRICED (were dropped → the recorded realtime $ was undercounted).
  #2  The paged admin-usage fetch FAILS LOUD at its page cap instead of silently truncating (a truncated slice
      reads as "less realtime spend" — the exact silent-undercount this module exists to prevent).
  #3  A malformed usage bucket (no `starting_at`) / a segment with no session id is SKIPPED with a trace, never
      KeyError-aborting the WHOLE oracle (one bad row must not drop every good one).
Pure/offline — network + provider SDKs stubbed; no LLM call.
"""
import os
import sys
import tempfile
import json

os.environ.setdefault("SPENDGUARD_HOME", tempfile.mkdtemp(prefix="sg-rtoracle-"))
os.environ.setdefault("SPENDGUARD_TEST_ISOLATED", "1")
os.environ.setdefault("SPENDGUARD_NO_AUTOINSTALL", "1")   # pure pricing + parsing; no SDK gate needed

from spendguard import pricing, realtime_oracle as ro


def ck(name, cond):
    ok = bool(cond)
    print(("  [OK] " if ok else "  [FAIL] ") + name)
    return [] if ok else [name]


def _raises(fn, exc):
    try:
        fn()
        return False
    except exc:
        return True
    except Exception:
        return False


fails = []

print("-- #1 cache-CREATION tokens are priced (ccreate x in_rate x CACHE_WRITE_5M_MULTIPLIER) --")
M = "gpt-5.5"   # a model pricing.verify() guarantees is in the table (in_=5.00); the formula is model-agnostic
base = pricing.realtime_cost(M, 1000, 100)
withcc = pricing.realtime_cost(M, 1000, 100, cache_creation_tok=2000)
exp_delta = 2000 * pricing.price(M)["in_"] * pricing.CACHE_WRITE_5M_MULTIPLIER / 1_000_000
fails += ck("cache-creation ADDS cost (not billed as free)", withcc > base)
fails += ck("cache-creation priced exactly at ccreate x in_rate x 1.25", abs((withcc - base) - exp_delta) < 1e-12)
fails += ck("default cache_creation_tok=0 leaves every existing caller unchanged",
            pricing.realtime_cost(M, 1000, 100) == pricing.realtime_cost(M, 1000, 100, cache_creation_tok=0))
fails += ck("cost_or_unpriced threads cache_creation_tok through",
            pricing.cost_or_unpriced(M, 1000, 100, batch=False, cache_creation_tok=2000)
            > pricing.cost_or_unpriced(M, 1000, 100, batch=False))

print("-- #2 paged admin fetch FAILS LOUD at the page cap (no silent truncation) --")
class _Resp:
    def __init__(self, body):
        self._b = body
    def read(self):
        return self._b
    def __enter__(self):
        return self
    def __exit__(self, *a):
        return False

# a provider that NEVER stops paginating → the loop must RAISE at max_pages, not return a truncated slice
ro.urllib.request.urlopen = lambda req, timeout=None: _Resp(
    json.dumps({"data": [{"i": 1}], "has_more": True, "next_page": "p"}).encode())
fails += ck("infinite pagination raises (truncation is loud, not a silent undercount)",
            _raises(lambda: ro._paged("http://x?a=1", {}, "page", max_pages=3), RuntimeError))
ro.urllib.request.urlopen = lambda req, timeout=None: _Resp(json.dumps({"data": [{"i": 1}], "has_more": False}).encode())
fails += ck("terminating pagination returns the data normally", ro._paged("http://x?a=1", {}, "page") == [{"i": 1}])

print("-- #3 a malformed bucket / sid-less segment is SKIPPED, never aborts the oracle --")
import spendguard.config as _cfg
import spendguard.resources as _res
_cfg.api_key = lambda k: "fake-admin-key"        # so anthropic_hourly proceeds past the key gate
_res._norm_model = lambda m: m
ro._paged = lambda url, headers, page_param, max_pages=80: [
    {"starting_at": "2026-08-01T00:00:00Z", "results": [
        {"model": "claude-opus-4-8", "uncached_input_tokens": 100, "cache_read_input_tokens": 10,
         "output_tokens": 50, "cache_creation_input_tokens": 20}]},
    {"results": [{"model": "x", "output_tokens": 999}]},         # MALFORMED: no starting_at
]
_by, _by_ok = None, True
try:
    _by = ro.anthropic_hourly("2026-08-01")
except Exception:
    _by_ok = False
fails += ck("anthropic_hourly skips the malformed bucket without raising (keeps the good one)",
            _by_ok and _by is not None and len(_by) == 1)
_rows = list(_by.values())[0] if _by else []
fails += ck("the good anthropic row carries the cache-creation slot (5-tuple, cc=20)",
            bool(_rows) and len(_rows[0]) == 5 and _rows[0][4] == 20)

import spendguard.conv as _conv
_conv.segments = lambda: [{"ts": "2026-08-01T01:00:00Z"},                          # no sid
                          {"ts": "2026-08-01T01:00:00Z", "sid": "s1"}]
_conv.session_classification = lambda sid: {"org": "o", "project": "p"}
_ch, _ch_ok = None, True
try:
    _ch = ro._conversation_hours("2026-08-01")
except Exception:
    _ch_ok = False
fails += ck("_conversation_hours skips the sid-less segment without KeyError-aborting", _ch_ok and _ch is not None)

print("-- bonus: lane 'unsuitable' WITHIN proven-good size leaves the ceiling unset (the bare-index KeyError) --")
from spendguard import adapters
adapters._lane_ok_max["ttlane"] = 10_000                 # the lane has answered a 10k-char prompt before
adapters._lane_big_prompt_ceiling.pop("ttlane", None)
_kind = adapters._learn_from_fallback("ttlane", "x" * 100, api_failed=False)   # a SMALL prompt fails: content-specific
fails += ck("a within/no-proven failure is 'model-cooled' and learns NO size ceiling", _kind == "model-cooled")
fails += ck("so the ceiling read must be .get (bare index would KeyError, as it did in the lane-fallback log)",
            adapters._lane_big_prompt_ceiling.get("ttlane") is None)

print(f"\n{'[FAIL]' if fails else 'OK'} test_realtime_oracle_hardening: {len(fails)} failure(s)")
sys.exit(1 if fails else 0)

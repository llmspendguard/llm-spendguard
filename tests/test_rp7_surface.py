"""RP7 surface fixes (warden): sig= keyed per-model, served_by_metered_api, and an un-intented-paid-call signal.

GAP 3 — adapters.call(sig=<intent>) was used RAW as the bulkgate key, pooling the measured p99 across models
(gpt-5-nano and claude-opus-4-8 shared one output profile). Now the key is bulkgate.sig(model, template_id=sig) —
per-model. GAP 4 — `billed` is cost>0 (true for a costing key-lane too), which is not "served by the metered API";
`served_by_metered_api` is. GAP 6 — a PAID call with no intent lands in '(none)' silently; now it warns (once per
site) and, under SPENDGUARD_REQUIRE_INTENT=1, raises on EVERY such call. Offline: adapters internals stubbed.
"""
import os
import sys
import tempfile
import warnings

os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="sg-rp7-")
os.environ.setdefault("SPENDGUARD_TEST_ISOLATED", "1")
os.environ.setdefault("SPENDGUARD_NO_AUTOINSTALL", "1")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from spendguard import adapters, bulkgate, calls

fails = []


def ck(name, cond):
    print(("  [OK] " if cond else "  [FAIL] ") + name)
    if not cond:
        fails.append(name)


# ── GAP 3: sig= is keyed PER MODEL, not pooled ──
_seen_keys = []
_orig_mt = bulkgate.maxtokens
bulkgate.maxtokens = lambda s, **k: (_seen_keys.append(s), {})[1]
adapters._call_once = lambda model, prompt, **kw: {"text": "ok", "cost": 0.0, "in_tok": 1, "out_tok": 1,
                                                   "executor": "api", "provider": model.split(":")[0],
                                                   "model": model.split(":")[1], "finish_reason": "stop"}
adapters.call("openai:gpt-5-nano", "hello", sig="warden:describe", max_tokens=64)
adapters.call("anthropic:claude-opus-4-8", "hello", sig="warden:describe", max_tokens=64)
bulkgate.maxtokens = _orig_mt
ck("sig= keyed per MODEL — same intent on two models → DIFFERENT keys (not pooled)",
   len(_seen_keys) == 2 and _seen_keys[0] != _seen_keys[1])
ck("...and the key IS bulkgate.sig(model, template_id=sig) (matches register-side keying)",
   _seen_keys[0] == bulkgate.sig("openai:gpt-5-nano", template_id="warden:describe"))

# ── GAP 4: served_by_metered_api distinguishes metered-served from merely-billed ──
def _res(executor, cost):
    return lambda model, prompt, **kw: {"text": "ok", "cost": cost, "in_tok": 1, "out_tok": 1,
                                        "executor": executor, "provider": "x", "model": "m", "finish_reason": "stop"}


adapters._call_once = _res("api", 0.01)
ck("executor='api' → served_by_metered_api True", adapters.call("openai:gpt-5-nano", "p", max_tokens=8)["served_by_metered_api"] is True)
adapters._call_once = _res("api-fallback", 0.01)
ck("executor='api-fallback' → True", adapters.call("openai:gpt-5-nano", "p", max_tokens=8)["served_by_metered_api"] is True)
adapters._call_once = _res("zai-coding", 0.02)
ck("a COSTING lane (executor='zai-coding', cost>0) → served_by_metered_api False (billed≠metered-served)",
   adapters.call("zai:glm-5.3", "p", max_tokens=8)["served_by_metered_api"] is False)
adapters._call_once = _res("codex", 0.0)
ck("a $0 lane → served_by_metered_api False", adapters.call("openai:gpt-5.6-sol", "p", max_tokens=8)["served_by_metered_api"] is False)

# ── GAP 6: an un-intented PAID call warns (once) and, under SPENDGUARD_REQUIRE_INTENT, raises on EVERY call ──
calls.set_context(intent=None)
with warnings.catch_warnings(record=True) as w:
    warnings.simplefilter("always")
    calls.record_call("openai", "gpt-5-nano", "realtime", 0.01)               # paid + no intent
    ck("a PAID call with no intent WARNS", any("NO intent" in str(x.message) for x in w))

with warnings.catch_warnings(record=True) as w2:
    warnings.simplefilter("always")
    calls.record_call("openai", "gpt-5-nano", "realtime", 0.0)                # $0 + no intent → no warn
    ck("a $0 call with no intent does NOT warn (only paid calls)", not any("NO intent" in str(x.message) for x in w2))

calls.set_context(intent="warden:describe")
with warnings.catch_warnings(record=True) as w3:
    warnings.simplefilter("always")
    calls.record_call("openai", "gpt-5-nano", "realtime", 0.01)               # paid + intent → no warn
    ck("a PAID call WITH an intent does NOT warn", not any("NO intent" in str(x.message) for x in w3))

calls._local.ctx = {}                                                         # set_context(None) is a no-op — hard reset
os.environ["SPENDGUARD_REQUIRE_INTENT"] = "1"
raised = 0
for _ in range(2):                                                            # must raise EVERY time, not just once
    try:
        calls.record_call("openai", "gpt-5-nano", "realtime", 0.01)
    except ValueError:
        raised += 1
ck("SPENDGUARD_REQUIRE_INTENT=1 raises on EVERY un-intented paid call (enforcement never lapses)", raised == 2)
del os.environ["SPENDGUARD_REQUIRE_INTENT"]
calls._local.ctx = {}

# ── FIRST (double-key rescue): a PRE-BUILT bulkgate.sig (16-hex) is used AS-IS, never re-wrapped into a sig-of-a-sig ──
_seen2 = []
_orig_mt2 = bulkgate.maxtokens
bulkgate.maxtokens = lambda s, **k: (_seen2.append(s), {})[1]
adapters._call_once = lambda model, prompt, **kw: {"text": "ok", "cost": 0.0, "in_tok": 1, "out_tok": 1,
                                                   "executor": "api", "provider": model.split(":")[0],
                                                   "model": model.split(":")[1], "finish_reason": "stop"}
_prebuilt = bulkgate.sig("openai:gpt-5-nano", template_id="warden:describe")   # a real 16-hex sig
adapters.call("openai:gpt-5-nano", "p", sig=_prebuilt, max_tokens=64)
bulkgate.maxtokens = _orig_mt2
ck("a pre-built bulkgate.sig (16-hex) is used AS-IS — not double-keyed (rescues the consumer workaround)",
   _seen2 and _seen2[0] == _prebuilt)

# ── GAP 1: schema= → the result carries the DECODED object as `parsed` (None if undecodable; absent without schema) ──
SCH = {"type": "object", "required": ["x"], "properties": {"x": {"type": "string"}}}


def _text_res(txt):
    return lambda model, prompt, **kw: {"text": txt, "cost": 0.0, "in_tok": 1, "out_tok": 1, "executor": "api",
                                        "provider": "o", "model": "m", "finish_reason": "stop"}


adapters._call_once = _text_res('{"x":"hi"}')
ck("schema= → result carries `parsed` (the decoded object)",
   adapters.call("openai:gpt-5-nano", "p", schema=SCH, max_tokens=64).get("parsed") == {"x": "hi"})
adapters._call_once = _text_res("not json at all")
ck("undecodable text → parsed is None (not silent nothing)",
   adapters.call("openai:gpt-5-nano", "p", schema=SCH, max_tokens=64).get("parsed") is None)
adapters._call_once = _text_res("plain")
ck("no schema → no `parsed` key (purely additive)", "parsed" not in adapters.call("openai:gpt-5-nano", "p", max_tokens=64))

# ── GAP 1 on the bulk row: the decoded object rides the row so the demux scatters the OBJECT, not a re-parse ──
from spendguard import lane_catalog, lane_bandit, lane_economics, dispatch as _dispatch
lane_catalog.arms = lambda flt=None: [("codex", "gpt-5.6-luna")]
lane_catalog.lane_provider = lambda l: "openai"
lane_bandit._arm_cooling = lambda l, u: False
lane_bandit.arm_stats = lambda intent: {("codex", "gpt-5.6-luna"): {"winrate": 1.0, "trials": 2}}
lane_economics.prompt_lane_reserved = lambda lane: False
adapters._lane_cooling = lambda ln: False
_dispatch.acquire = lambda *a, **k: 0.0
_dispatch.release = lambda *a, **k: None
adapters.call = lambda model, prompt, **kw: {"text": '{"x":"hi"}', "parsed": {"x": "hi"}, "cost": 0,
                                             "executor": "codex", "provider": "openai", "model": "gpt-5.6-luna"}
from spendguard import lane_balance
_rows = lane_balance.bulk_delegate(["t"], "rp7:parsed", schema=SCH)
ck("bulk_delegate row carries `parsed` from the decoded envelope", _rows[0].get("parsed") == {"x": "hi"})

print(("[OK]" if not fails else "[FAIL]") + " rp7 surface: %d failure(s)" % len(fails))
sys.exit(1 if fails else 0)

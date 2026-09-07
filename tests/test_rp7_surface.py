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

print(("[OK]" if not fails else "[FAIL]") + " rp7 surface: %d failure(s)" % len(fails))
sys.exit(1 if fails else 0)

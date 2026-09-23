"""Guard — the max_tokens / metered_only 'madness' fixes (resolve the bc_edges_v15 class completely):
  (1) metered_only ⟹ no_substitution in adapters.call — a metered_only call pins the model, so best-value / a lane
      bandit can't swap it onto a different vendor's $0 lane (the measured warden:tag_provenance_xcheck bypass:
      metered_only=True still got gpt-5-nano → opus via the claude-code lane);
  (2) bulk_delegate(metered_only=True) WITHOUT a pinned model (model_for) or vision RAISES — never silently ignores
      metered_only and drops to the lane fan (where the bandit substitutes);
  (3) a small explicit max_tokens on a STRUCTURED call is FLOORED and WARNED once (teach consumers to stop passing
      max_tokens — the #1 cause of silent reasoning-model JSON truncation).
Hermetic: _call_guarded + the resolver stubbed; routing off; no network."""
import os
import sys
import tempfile

os.environ.setdefault("SPENDGUARD_TEST_ISOLATED", "1")
os.environ.setdefault("SPENDGUARD_HOME", tempfile.mkdtemp(prefix="spendguard-mtok-"))
os.environ["SPENDGUARD_ROUTE_THROUGH_QUEUE"] = "0"   # isolate: don't route these probe calls through the queue

from spendguard import adapters, lane_balance

_fails = []
def ck(label, cond):
    if not cond:
        _fails.append(label)
    print(f"  [{'OK' if cond else 'FAIL'}] {label}")

# ── (1) metered_only ⟹ no_substitution: _call_guarded receives _no_sub=True ──
print("-- (1) metered_only ⟹ no_substitution (pins the model against a lane/bandit swap) --")
adapters._resolve_guard.on = True                    # skip the served-substitute resolver (no network)
_seen = {}
_saved_guarded = adapters._call_guarded
def _stub_guarded(model, prompt, **kw):
    _seen["no_sub"] = kw.get("_no_sub")
    return {"text": "ok", "model": model, "provider": "x", "cost": 0.0, "in_tok": 1, "out_tok": 1, "error": None}
adapters._call_guarded = _stub_guarded
try:
    adapters.call("openai:gpt-x", "p", sig="mtok:t", metered_only=True)
    ck("metered_only=True → _call_guarded gets _no_sub=True (model pinned)", _seen.get("no_sub") is True)
    _seen.clear()
    adapters.call("openai:gpt-x", "p", sig="mtok:t", metered_only=False, no_substitution=False)
    ck("without metered_only, no_substitution stays False (unchanged)", _seen.get("no_sub") is False)
finally:
    adapters._call_guarded = _saved_guarded
    adapters._resolve_guard.on = False

# ── (2) bulk_delegate(metered_only=True) without a pinned model RAISES (never silently lane-fans) ──
print("-- (2) bulk_delegate(metered_only=True) without model_for/images_for RAISES --")
try:
    lane_balance.bulk_delegate([{"id": "a"}], "mtok:intent", metered_only=True, force=True)
    ck("metered_only without a pinned model raised", False)
except ValueError as e:
    ck("metered_only without a pinned model raises ValueError", "metered_only" in str(e) and "pin" in str(e).lower())

# ── (3) the caller-max_tokens warn: LOUD once per call-class, deduped ──
print("-- (3) _warn_once_caller_maxtokens: floors + warns once per call-class --")
adapters._MAXTOK_WARN_LEDGER.discard("mtok:cls")
m1 = adapters._warn_once_caller_maxtokens("mtok:cls", 120, 32000)
ck("first small-max_tokens on a class → a LOUD warning is returned", bool(m1) and "OMIT max_tokens" in m1)
ck("the warning names the passed value and names the fix", "120" in m1 and "spendguard owns the output ceiling" in m1)
m2 = adapters._warn_once_caller_maxtokens("mtok:cls", 120, 32000)
ck("same class again → deduped (no repeat warning)", m2 is None)

print(f"\n{'[FAIL]' if _fails else 'OK'} test_metered_only_and_maxtok_guards: {len(_fails)} failure(s)")
sys.exit(1 if _fails else 0)

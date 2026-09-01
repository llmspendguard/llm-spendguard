"""The FIXED, TESTED MAP for accurate calling — preflight + verify — plus the one-concept containment fix that lets
them halt correctly. Pins three things so they can't silently rot:

  1. model_preflight resolves EXPLICIT STATES (served / stale / unchecked / unknown-provider / unpriced / empty),
     never binary: a stale id is NOT usable-as-written even when a correction exists (dispatch refuses a stale id, it
     does not auto-swap) but the correction is NAMED; an 'unchecked' (catalog not synced) id IS usable (a can't-know
     is not a no); every input spec yields exactly one row (no silent drop).
  2. verify_system's PASS is the AND of every structural check — one ✗ anywhere fails the whole verdict.
  3. gate.deliberate_stop_types() is the SINGLE concept a fail-open handler re-raises: BudgetRefused now SUBCLASSES
     SpendGateRefused (no enumeration hole), and DispatchTimeout is enumerated as the one non-refusal deliberate stop.

Hermetic: the catalog/pricing/sub-checks are monkeypatched — no network, no isolated home, no re-exec, zero spend."""
import sys

from spendguard import model_preflight as mp, verify, gate, vendor_call, pricing
from spendguard.gate import SpendGateRefused
from spendguard.crossllm import BudgetRefused
from spendguard.dispatch import DispatchTimeout

fails = []
def ck(name, cond):
    print(("  [OK] " if cond else "  [FAIL] ") + name)
    if not cond:
        fails.append(name)

# ── 1. the one deliberate-stop CONCEPT (the containment fix) ─────────────────────────────────────────────────────
ck("BudgetRefused SUBCLASSES SpendGateRefused (closes the enumeration hole)", issubclass(BudgetRefused, SpendGateRefused))
stop_types = gate.deliberate_stop_types()
ck("deliberate_stop_types() includes SpendGateRefused", SpendGateRefused in stop_types)
ck("deliberate_stop_types() includes DispatchTimeout (the non-refusal deliberate stop)", DispatchTimeout in stop_types)
ck("a BudgetRefused instance is caught by the concept (via subclassing)", isinstance(BudgetRefused(1.0, 0.5, {}), stop_types))
ck("a plain ValueError is NOT a deliberate stop", not isinstance(ValueError("x"), stop_types))

# ── 2. preflight EXPLICIT STATES (hermetic: fake the catalog + pricing) ──────────────────────────────────────────
_served = {"gemini:gemini-real": "served", "gemini:gemini-stale": "stale",
           "openai:gpt-unpriced": "served", "openai:gpt-uncheck": "unchecked"}
_priced = {"gemini-real", "gemini-fixed", "gpt-uncheck"}      # 'gpt-unpriced' deliberately absent

_orig = (vendor_call.served_check, vendor_call.closest_served, pricing.realtime_cost)
vendor_call.served_check = lambda prov, mid: _served.get(f"{prov}:{mid}", "served")
vendor_call.closest_served = lambda prov, mid: (("gemini-fixed", []) if mid == "gemini-stale" else (None, []))
def _fake_price(model, in_tok, out_tok=0, **k):
    if model in _priced:
        return 0.001
    raise KeyError(model)                                    # the UNPRICED signal preflight catches narrowly
pricing.realtime_cost = _fake_price
try:
    specs = ["gemini:gemini-real", "gemini:gemini-stale", "openai:gpt-unpriced", "openai:gpt-uncheck", "", ":orphan"]
    rows = mp.preflight_models(specs)
    by = {r["spec"]: r for r in rows}
    ck("EVERY spec produced a row — len(out) == len(specs), no silent drop", len(rows) == len(specs))
    ck("served + priced → usable", by["gemini:gemini-real"]["usable"] is True)
    ck("STALE → NOT usable as-written, but the correction is named",
       by["gemini:gemini-stale"]["usable"] is False and by["gemini:gemini-stale"]["corrected"] == "gemini-fixed")
    ck("served but UNPRICED → NOT usable", by["openai:gpt-unpriced"]["usable"] is False)
    ck("UNCHECKED + priced → usable (a can't-know is not a no)", by["openai:gpt-uncheck"]["usable"] is True)
    ck("empty spec → surfaced 'empty' row, not usable", by[""]["served"] == "empty" and by[""]["usable"] is False)
    ck("no provider → surfaced 'unknown-provider' row", by[":orphan"]["served"] == "unknown-provider")
finally:
    vendor_call.served_check, vendor_call.closest_served, pricing.realtime_cost = _orig

# ── 3. verify_system PASS is the AND of every structural check ───────────────────────────────────────────────────
import spendguard.lane_catalog as lc
import spendguard.model_preflight as mpmod
import spendguard.lane_economics as le

_savers = {"pf": mpmod.preflight_models, "cs": mpmod.configured_specs, "fb": lc.audit_lane_fallback,
           "lanes": lc.lanes, "prov": lc.lane_provider, "econ": le.economics, "kp": verify._key_present}
def _install(preflight_ok, fallback_ok, key_ok):
    mpmod.configured_specs = lambda: ["x:y"]
    mpmod.preflight_models = lambda specs, correct=True: [{"spec": "x:y", "usable": preflight_ok, "note": "t",
                                                           "served": "served", "corrected": None, "priced": True}]
    lc.audit_lane_fallback = lambda: [{"lane": "l", "use_name": "u", "metered_id": "m", "served": "served",
                                       "priced": True, "ok": fallback_ok}]
    lc.lanes = lambda: ["l"]
    lc.lane_provider = lambda ln: "openai"
    le.economics = lambda: []
    verify._key_present = lambda prov: (key_ok, "OPENAI_API_KEY")
try:
    _install(True, True, True)
    ck("verify PASS when every structural check passes", verify.verify_system(probe=False)["ok"] is True)
    _install(False, True, True)
    ck("verify FAILS when a model id is not usable", verify.verify_system(probe=False)["ok"] is False)
    _install(True, False, True)
    ck("verify FAILS when a lane's failover map is broken", verify.verify_system(probe=False)["ok"] is False)
    _install(True, True, False)
    ck("verify FAILS when a provider key is missing (fallback would strand)", verify.verify_system(probe=False)["ok"] is False)
finally:
    mpmod.preflight_models = _savers["pf"]; mpmod.configured_specs = _savers["cs"]
    lc.audit_lane_fallback = _savers["fb"]; lc.lanes = _savers["lanes"]; lc.lane_provider = _savers["prov"]
    le.economics = _savers["econ"]; verify._key_present = _savers["kp"]

print(("\n[OK] " if not fails else "\n[FAIL] ") + "preflight_and_verify: %d failure(s)" % len(fails))
sys.exit(1 if fails else 0)

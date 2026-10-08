"""lane_servability: the MEASURED served/rejected verdict + the loud SHIFT detector that caught the 2026-10-06
codex-cli 0.160.1 drop of gpt-5.6-sol. Offline — calls.lane_model_outcomes, vendor_call (/models + closest_served)
and the macOS notifier are all stubbed; the catalog merge writes to an isolated temp HOME. Zero spend.

Pins the policy that must never silently regress: a model is SERVED only with a recent success; REJECTED only on a
wall of recent failures with ZERO recent successes; a loud SHIFT only when it ALSO had real prior successes (so a
model that NEVER worked is not shouted about); and the /models cross-check tells a lane/plan drop (still metered)
from a provider-wide drop (gone) from a rename (a near-match appeared) — never a guess."""
import os
import sys
import tempfile

if not os.environ.get("SPENDGUARD_TEST_ISOLATED"):
    os.environ["SPENDGUARD_TEST_ISOLATED"] = "1"
    os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="spendguard-laneserv-")
    os.execv(sys.executable, [sys.executable] + sys.argv)

from spendguard import lane_servability as ls, catalog, calls, vendor_call, reliability

fails = []


def ck(name, cond):
    print(("  [OK] " if cond else "  [FAIL] ") + name)
    if not cond:
        fails.append(name)


# Deterministic thresholds for the test, independent of any config default (set the env knobs the module reads).
os.environ["SPENDGUARD_LANE_SERVABILITY_MIN_FAIL"] = "10"
os.environ["SPENDGUARD_LANE_SERVABILITY_MIN_PRIOR_OK"] = "50"
os.environ["SPENDGUARD_LANE_SERVABILITY_SHIFT_FAIL"] = "20"

# ── 1. observed_lane_models: the served / rejected / shifted classification from MEASURED outcomes ──
# recent window vs all-time are two calls distinguished by `since` (None = all-time). The stub returns a fixed world:
#   served-model : recent successes → SERVED
#   dropped-model: 0 recent ok, 496 recent fail, 7132 all-time ok → REJECTED + SHIFTED (used to work, loud)
#   neverworked  : 0 recent ok, 496 recent fail, 0 all-time ok  → REJECTED, NOT shifted (never worked, not shouted)
#   thin-model   : 0 recent ok, 3 recent fail                   → NEITHER (too little evidence — never a guess)
_RECENT = {"served-model": {"ok": 2009, "fail": 13, "last_ok": "2026-10-07T18:00", "last_fail": None},
           "dropped-model": {"ok": 0, "fail": 496, "last_ok": None, "last_fail": "2026-10-07T18:00"},
           "neverworked": {"ok": 0, "fail": 496, "last_ok": None, "last_fail": "2026-10-07T18:00"},
           "thin-model": {"ok": 0, "fail": 3, "last_ok": None, "last_fail": "2026-10-07T18:00"}}
_ALLTIME = {"served-model": {"ok": 9000, "fail": 20},
            "dropped-model": {"ok": 7132, "fail": 496},   # a real history of success → a drop here is a SHIFT
            "neverworked": {"ok": 0, "fail": 496},          # never once served → rejected, but not a shift
            "thin-model": {"ok": 0, "fail": 3}}


def _fake_outcomes(executor, since=None, call_class=None):
    return _ALLTIME if since is None else _RECENT


calls.lane_model_outcomes = _fake_outcomes

o = ls.observed_lane_models("codex")
ck("served = models with a recent success", o["served"] == ["served-model"])
ck("rejected = zero recent ok + ≥min_fail recent fails (both dropped & never-worked)",
   set(o["rejected"]) == {"dropped-model", "neverworked"})
ck("shifted = rejected AND had real prior successes (dropped only, NOT never-worked)",
   o["shifted"] == ["dropped-model"])
ck("a thin failure history is NEITHER served nor rejected (never a guess)",
   "thin-model" not in o["served"] and "thin-model" not in o["rejected"])

# ── 2. the /models cross-check classifies the drop: lane-drop vs provider-drop vs rename vs unknown ──
vendor_call.closest_served = lambda vendor, m: None        # default: no near-match
ck("still on the metered /models list → LANE-drop (the plan dropped it; the 10-06 case)",
   ls._classify_drop("openai", "gpt-5.6-sol", ["gpt-5.6-sol", "gpt-6-sol"])["kind"] == "lane-drop")
ck("gone from the metered list, no near-match → PROVIDER-drop",
   ls._classify_drop("openai", "gpt-5.6-sol", ["gpt-6-sol"])["kind"] == "provider-drop")
vendor_call.closest_served = lambda vendor, m: "gpt-5.6-sol-v2"
_rn = ls._classify_drop("openai", "gpt-5.6-sol", ["gpt-6-sol"])
ck("gone from the metered list but a near-match exists → RENAME, carrying the replacement",
   _rn["kind"] == "rename" and _rn["replacement"] == "gpt-5.6-sol-v2")
ck("metered list unavailable (None) → UNKNOWN, never a false 'gone'",
   ls._classify_drop("openai", "gpt-5.6-sol", None)["kind"] == "unknown")

# ── 3. detect_served_shift: raises LOUD (through reliability) only for a shift at/above the shift_fail floor ──
vendor_call.closest_served = lambda vendor, m: None
ls._provider_models_now = lambda prov: ["dropped-model", "served-model"]   # stub /models: dropped-model STILL metered → lane-drop
ls._lanes = lambda: {"codex": "openai"}
_alerts = []
reliability.note_lane_model_shift = lambda lane, model, diag: _alerts.append((lane, model, diag))
shifts = ls.detect_served_shift("codex")
ck("a shift at ≥shift_fail is detected and classified lane-drop", len(shifts) == 1 and shifts[0]["kind"] == "lane-drop")
ck("the loud alert fired once through reliability with the model named",
   len(_alerts) == 1 and _alerts[0][1] == "dropped-model")
ck("the diagnosis is actionable (names the lane-drop + the fix command)",
   "plan dropped" in _alerts[0][2] and "set-model" in _alerts[0][2])

# a shift BELOW the loud floor is not shouted (recorded quietly by refresh instead)
os.environ["SPENDGUARD_LANE_SERVABILITY_SHIFT_FAIL"] = "1000"
_alerts.clear()
ck("below the loud floor → no shout", ls.detect_served_shift("codex") == [] and _alerts == [])
os.environ["SPENDGUARD_LANE_SERVABILITY_SHIFT_FAIL"] = "20"

# ── 4. merge_lane_observation: served ∪ prior (never drops agy's set), rejected removed from served, two sets agree ──
catalog.merge_lane_observation("codex", ["gpt-6-sol"], [], asof="2026-10-07T00:00:00+00:00")      # seed a prior served id
catalog.merge_lane_observation("codex", ["gpt-5.6-luna"], ["gpt-5.6-sol"], asof="2026-10-07T19:00:00+00:00")
served = set(catalog.lane_model_ids("codex") or [])
unserved = set(catalog.lane_unserved_ids("codex") or [])
ck("served UNIONS with the prior set (agy-pulled ids never dropped)", {"gpt-6-sol", "gpt-5.6-luna"} <= served)
ck("rejected id is recorded unserved AND absent from served (the two sets never disagree)",
   "gpt-5.6-sol" in unserved and "gpt-5.6-sol" not in served)

# ── 5. lane_served_substitute: resolve a lane-rejected id to a SERVED lane model (deterministic, strong-first) ──
# Catalog state from section 4: codex served {gpt-6-sol, gpt-5.6-luna}, unserved {gpt-5.6-sol}. Stub the operator's
# per-tier config so the strong-first preference is exercised (no config in the isolated HOME otherwise).
from spendguard import lane_catalog
lane_catalog.lane_model_for_tier = lambda lane, tier: {"strong": "gpt-6-sol", "cheap": "gpt-5.6-luna"}.get(tier)
sub, why = ls.lane_served_substitute("codex", "gpt-5.6-sol")
ck("a lane-rejected model resolves to the lane's STRONG served model (never a capability downgrade)", sub == "gpt-6-sol")
ck("the resolution reason names the swap (not silent)", "no longer serves" in (why or ""))
ck("a model the lane DOES serve is left unchanged (nothing to resolve)",
   ls.lane_served_substitute("codex", "gpt-6-sol") == ("gpt-6-sol", None))
ck("an unknown lane → unchanged (never invents a substitute)",
   ls.lane_served_substitute("nope-not-a-lane", "gpt-5.6-sol") == ("gpt-5.6-sol", None))
# with NO served candidate left, it does not swap — the caller then takes the honest metered path
catalog.merge_lane_observation("zzz-lane", [], ["only-model"], asof="2026-10-07T00:00:00+00:00")
ck("no served replacement → unchanged (honest metered fallback upstream, never a phantom)",
   ls.lane_served_substitute("zzz-lane", "only-model") == ("only-model", None))

# ── 6. lane_readiness: MEASURED + model-specific; 'degraded' trips ONLY on an operator-configured floor (defect 2) ──
# Recent world (stub): served-model 2009 ok; dropped/neverworked 496 fail each; thin 3 fail → overall rate ≈ 0.67.
os.environ.pop("SPENDGUARD_LANE_SERVABILITY_OK_RATE_FLOOR", None)
rd = ls.lane_readiness("codex")
ck("readiness names the rejected models (measurement pattern, no hand-picked bound)",
   set(rd["rejected"]) == {"dropped-model", "neverworked"})
ck("no operator floor configured → never 'degraded' (code chooses no acceptable-rate cutoff)", rd["degraded"] is False)
os.environ["SPENDGUARD_LANE_SERVABILITY_OK_RATE_FLOOR"] = "0.9"      # the OPERATOR sets a strict floor
ck("recent rate below the OPERATOR's configured floor → degraded", ls.lane_readiness("codex")["degraded"] is True)
os.environ["SPENDGUARD_LANE_SERVABILITY_OK_RATE_FLOOR"] = "0.1"      # the operator sets a lenient floor
ck("recent rate above the operator's floor → not degraded", ls.lane_readiness("codex")["degraded"] is False)
os.environ.pop("SPENDGUARD_LANE_SERVABILITY_OK_RATE_FLOOR", None)

# ── 7. reprobe_rejected: a recovered model self-heals (clears unserved); a still-dead one stays rejected ──
import types as _types
catalog.merge_lane_observation("codex", ["gpt-6-sol"], ["came-back", "still-dead"], asof="2026-10-07T20:00:00+00:00")
_PROBE_RESULT = {"came-back": {"text": "ok"}, "still-dead": {"error": "not supported", "status_code": 400}}  # fixture data
def _fake_mod():
    m = _types.SimpleNamespace()
    m.run_prompt = lambda prompt, system=None, model=None, timeout=None, reasoning=None, max_tokens=None: \
        dict(_PROBE_RESULT[model])
    return m
ls._lane_module = lambda lane: _fake_mod()
reliability.note_lane_model_ok = lambda lane, model: None
import spendguard.calls as _calls_spy
_recorded = []
_calls_spy.record_call = lambda **kw: _recorded.append(kw)      # spy: the recovery must land in the LEDGER, not just caches
res = ls.reprobe_rejected("codex")
ck("a recovered model probes OK and moves out of unserved", res["recovered"] == ["came-back"])
ck("a still-dead model stays rejected", res["still"] == ["still-dead"])
ck("a recovery is RECORDED to the ledger as an ok (so observed_lane_models self-heals, banner stays cleared)",
   any(k.get("outcome") == "ok" and k.get("executor") == "codex" and k.get("model") == "came-back" for k in _recorded))
ck("a still-dead model is NOT recorded as ok (never a false recovery)",
   not any(k.get("model") == "still-dead" and k.get("outcome") == "ok" for k in _recorded))
ck("catalog reflects recovery: came-back no longer unserved, still-dead still unserved",
   "came-back" not in set(catalog.lane_unserved_ids("codex") or [])
   and "still-dead" in set(catalog.lane_unserved_ids("codex") or []))

# ── 8. 2c: a codex DETERMINISTIC rejection (HTTP 400) is non-retryable; a transient (no status) stays retryable ──
from spendguard import codex_exec as _cx, vendor_call as _vc
ck("codex 400 status is PARSED from the documented payload",
   _cx._codex_error_status('{"type":"error","status":400,"error":{"type":"invalid_request_error"}}') == 400)
ck("a transient error carries no status → None (not forced non-retryable)",
   _cx._codex_error_status("codex lane timeout (40s)") is None)
ck("a codex 400 classifies NON-retryable (payload_rejected)",
   _vc._classify({"error": "unsupported model", "status_code": 400})[0] not in _vc.RETRYABLE)
ck("a codex transient (no status) classifies RETRYABLE (correct for the gpt-5.6-sol outage)",
   _vc._classify({"error": "codex lane timeout (40s)"})[0] in _vc.RETRYABLE)

print(("[OK]" if not fails else "[FAIL]") + " lane servability: %d failure(s)" % len(fails))
sys.exit(1 if fails else 0)

"""When the primary lane FAILS and no free substitute LANE is available, route_decision (reactive) falls to the
CHEAPEST AFFORDABLE confirmed METERED substitute — via route_utility.rank_metered — instead of paying full price on
the ORIGINAL model. This is the Phase-2 wiring of rank_metered into the reactive routing brain.

Invariants guarded here:
  • FREE lanes are ALWAYS preferred — a metered substitute is used ONLY when no substitute lane can serve.
  • It never leaves the user-CONFIRMED substitute set (default OFF: no confirmed subs → None → unchanged).
  • PROACTIVE routing never pays to fill idle plans — the metered fall is REACTIVE-only.
  • Cheapest-per-token wins; a substitute whose prepay can't cover the call is skipped, not picked; all-exhausted → None.
  • A 'metered' spec is one whose provider has NO subscription lane AND is a provider we can actually call.

Hermetic: isolated home, deterministic stubbed pricing + balances, zero spend, no network."""
import os, sys, tempfile

if not os.environ.get("SPENDGUARD_TEST_ISOLATED"):
    os.environ["SPENDGUARD_TEST_ISOLATED"] = "1"
    os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="spendguard-metsub-")
    os.execv(sys.executable, [sys.executable] + sys.argv)

from spendguard import lane_balance as LB
from spendguard import adapters, pricing, balances

fails = []
def ck(name, cond):
    print(("  [OK] " if cond else "  [FAIL] ") + name)
    if not cond:
        fails.append(name)

PRIMARY = "anthropic:claude-opus-4-8"          # the model the caller asked for; its lane (claude-code) just failed
CHEAP   = "deepseek:deepseek-chat"             # metered (deepseek has no subscription lane), cheapest
DEAR    = "moonshot:kimi-k3"                   # metered (moonshot has no lane), dearer
LANE    = "gemini:gemini-3-flash"              # a FREE substitute LANE (gemini maps to a lane)

# sanity: our fixtures match the lane/provider facts route_decision reads
ck("deepseek/moonshot are LANE-less callable providers (so they count as metered)",
   not adapters._LANES.get("deepseek") and not adapters._LANES.get("moonshot")
   and "deepseek" in adapters.PROVIDERS and "moonshot" in adapters.PROVIDERS)
ck("gemini DOES map to a subscription lane (so it is preferred as free, never metered)",
   bool(adapters._LANES.get("gemini")))

# ── deterministic stubs: cost ranking + balance availability (we test the SELECTION, not pricing/balances internals)
_COST = {CHEAP: 0.001, DEAR: 0.005}
pricing.realtime_cost = lambda spec, i, o: _COST.get(spec, 0.01)
balances._declared = lambda p: {}
_AVAIL = {}                                     # provider -> balance dict; default = on_demand (always affordable)
balances.vendor_balance = lambda p: _AVAIL.get(p, {"kind": "on_demand"})
# deterministic lane picture + no cooling by default
LB.lane_utilization = lambda: {"lanes": [
    {"lane": "claude-code", "utilization": 9.0, "calls_recent": 500},
    {"lane": "gemini", "utilization": 0.0, "calls_recent": 0},
    {"lane": "codex", "utilization": 0.0, "calls_recent": 0},
    {"lane": "zai-coding", "utilization": 0.0, "calls_recent": 0}]}
adapters._lane_cooling = lambda ln: False

# ── 1. FREE LANE PREFERRED: a live substitute lane beats every metered target ──
LB.confirm_substitute("mix", LANE); LB.confirm_substitute("mix", CHEAP); LB.confirm_substitute("mix", DEAR)
sub, why = LB.route_decision("mix", PRIMARY, reactive=True)
ck("free substitute LANE is chosen over cheaper metered targets", sub == LANE)

# ── 2. NO FREE LANE → cheapest AFFORDABLE metered substitute (reactive) ──
LB.confirm_substitute("metered-only", CHEAP); LB.confirm_substitute("metered-only", DEAR)
sub2, why2 = LB.route_decision("metered-only", PRIMARY, reactive=True)
ck("no substitute lane → cheapest metered substitute (deepseek < moonshot)", sub2 == CHEAP)
ck("...and the reason names it a metered substitute after the failed lane", "metered substitute" in (why2 or ""))

# ── 3. PROACTIVE never pays to fill idle plans ──
sub3, _ = LB.route_decision("metered-only", PRIMARY, reactive=False)
ck("PROACTIVE with only metered substitutes → None (never pay to fill idle)", sub3 is None)

# ── 4. lanes COOLING → the free lane drops out, metered carries it ──
adapters._lane_cooling = lambda ln: True                       # every lane cooling now
sub4, _ = LB.route_decision("mix", PRIMARY, reactive=True)
ck("all lanes cooling → falls to cheapest metered substitute", sub4 == CHEAP)
adapters._lane_cooling = lambda ln: False

# ── 5. cheapest metered EXHAUSTED (sunk pool can't cover it) → next affordable ──
_AVAIL["deepseek"] = {"kind": "sunk_pool", "available": 0.0}    # deepseek prepay can't cover the call
sub5, why5 = LB.route_decision("metered-only", PRIMARY, reactive=True)
ck("exhausted cheapest is SKIPPED, next affordable metered chosen (moonshot)", sub5 == DEAR)

# ── 6. ALL metered exhausted → None (caller then pays full price on the ORIGINAL model, unchanged) ──
_AVAIL["moonshot"] = {"kind": "sunk_pool", "available": 0.0}
sub6, _ = LB.route_decision("metered-only", PRIMARY, reactive=True)
ck("every metered substitute exhausted → None (original model's API is the final fallback)", sub6 is None)
_AVAIL.clear()

# ── 7. a non-callable provider is filtered (never handed a spec we can't dispatch) ──
LB.confirm_substitute("bogus-only", "nosuchprovider:x")
sub7, _ = LB.route_decision("bogus-only", PRIMARY, reactive=True)
ck("a metered spec for an unknown provider is filtered out → None", sub7 is None)

# ── 8. DEFAULT OFF: an intent with no confirmed substitute is unchanged ──
sub8, _ = LB.route_decision("never-configured", PRIMARY, reactive=True)
ck("no confirmed substitute → None (feature is opt-in per intent, nothing routes)", sub8 is None)

print(("\n[OK] " if not fails else "\n[FAIL] ") + f"metered_substitute_fallback: {len(fails)} failure(s)")
sys.exit(1 if fails else 0)

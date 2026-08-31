"""On-cap-breach routing policy — the Cloudflare 'downgrade vs hard-block' choice, spendguard-shaped and FAIL-CLOSED
by default. On a budget breach the gate hard-refuses (the identity); the opt-in `caps.on_breach=downgrade` makes the
refusal ACTIONABLE by naming the cheapest idle $0 subscription lane to route to instead. The gate never silently
loosens: an unrecognised policy, or downgrade-set-but-no-lane, both resolve to refuse.

Guards route_utility.breach_policy/breach_decision AND that the gate's budget refusal carries the downgrade hint.
Offline, isolated home, zero spend."""
import os, sys, tempfile

if not os.environ.get("SPENDGUARD_TEST_ISOLATED"):
    os.environ["SPENDGUARD_TEST_ISOLATED"] = "1"
    os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="spendguard-breach-")
    os.execv(sys.executable, [sys.executable] + sys.argv)

for _k in ("SPENDGUARD_ON_BREACH", "GATE_ALLOW", "GATE_DISABLE"):
    os.environ.pop(_k, None)

from spendguard import route_utility as ru
from spendguard import lanes, gate, budget, config
from spendguard.gate import SpendGateRefused

fails = []
def ck(name, cond):
    print(("  [OK] " if cond else "  [FAIL] ") + name)
    if not cond:
        fails.append(name)

LANE_ROW = {"lane": "gemini", "provider": "gemini", "remaining_pct": 50, "reset_ts": None, "known": True}

print("-- (1) breach_policy: refuse by default; env/config sets downgrade; junk → refuse (never loosen) --")
ck("default policy is refuse (fail-closed)", ru.breach_policy() == "refuse")
os.environ["SPENDGUARD_ON_BREACH"] = "downgrade"
ck("env SPENDGUARD_ON_BREACH=downgrade → downgrade", ru.breach_policy() == "downgrade")
os.environ["SPENDGUARD_ON_BREACH"] = "yolo"
ck("an unrecognised policy resolves to refuse (never silently loosen)", ru.breach_policy() == "refuse")
os.environ.pop("SPENDGUARD_ON_BREACH", None)

print("-- (2) breach_decision: refuse names no target; downgrade names the cheapest idle $0 lane --")
ck("refuse → ('refuse', None, …)", ru.breach_decision("refuse")[:2] == ("refuse", None))
_orig_headroom = lanes.lane_headroom
lanes.lane_headroom = lambda do_fetch=True: [LANE_ROW]      # an idle lane WITH headroom
act, tgt, why = ru.breach_decision("downgrade")
ck("downgrade + an available lane → ('downgrade', that lane, why)", act == "downgrade" and tgt == "gemini")
ck("...the why names the $0 plan route", "gemini" in why and "$0" in why)
lanes.lane_headroom = lambda do_fetch=True: []             # no idle lane
ck("downgrade but NO idle lane has headroom → refuse (fail-closed, the safe direction)",
   ru.breach_decision("downgrade")[0] == "refuse")
lanes.lane_headroom = _orig_headroom

print("-- (3) the gate's BUDGET refusal carries the downgrade hint when policy=downgrade --")
# make _budget_check reach its raise: sqlite backend, not reading history, a real breach, non-interactive
_ob, _bb, _rh, _ex = config.budget_backend, budget.is_reading_history, None, budget.exceeded
config.budget_backend = lambda: "sqlite"
budget.is_reading_history = lambda: False
budget.exceeded = lambda cost, kind=None: ("llm-daily", 1.0, 5.0)   # (window, cap, projected) → a breach
lanes.lane_headroom = lambda do_fetch=True: [LANE_ROW]
try:
    os.environ["SPENDGUARD_ON_BREACH"] = "downgrade"
    msg = ""
    try:
        gate._budget_check(3.0, "gpt-5.5", "openai", "batch")
    except SpendGateRefused as e:
        msg = str(e)
    ck("budget breach RAISES SpendGateRefused (still fail-closed)", "would be exceeded" in msg)
    ck("...and the refusal NAMES the downgrade lane (on_breach=downgrade)",
       "on_breach=downgrade" in msg and "gemini" in msg)
    # refuse policy → no hint (the default hard-stop, unchanged)
    os.environ["SPENDGUARD_ON_BREACH"] = "refuse"
    try:
        gate._budget_check(3.0, "gpt-5.5", "openai", "batch")
    except SpendGateRefused as e:
        ck("refuse policy → the refusal carries NO downgrade hint (unchanged fail-closed message)",
           "on_breach=downgrade" not in str(e))
finally:
    config.budget_backend, budget.is_reading_history, budget.exceeded = _ob, _bb, _ex
    lanes.lane_headroom = _orig_headroom
    os.environ.pop("SPENDGUARD_ON_BREACH", None)

print(("\n[OK] " if not fails else "\n[FAIL] ") + f"breach_policy: {len(fails)} failure(s)")
sys.exit(1 if fails else 0)

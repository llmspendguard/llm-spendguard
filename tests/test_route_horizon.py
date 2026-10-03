"""Item C — HORIZON ROUTING. route_horizon.plan_horizon allocates a whole job SET across the subscription lane, the
metered BATCH API, and the metered REALTIME API, honouring caller-declared URGENCY and the SHARED, scarce lane budget.

Pins the policy (pure arithmetic, $0, no network):
  • URGENT overflow → metered REALTIME (fast), never batch; DEFERRABLE overflow → cheap cap-free BATCH, never meter.
  • the ~free/waste lane capacity (would expire at reset) is reclaimed first, at $0, for either kind.
  • the ONE shared lane budget is drawn down URGENT-FIRST — an urgent group banks the ~free tokens before a
    deferrable group (urgent-first is cost-optimal: a free token averts the costlier realtime overflow).
  • the interactive RESERVE is held out (lane tokens used ≤ remaining − reserve).
  • honest degrade: no converged lane → urgent→meter / deferrable→batch; no priced path → tokens reported UNPRICED
    (usd None), never invented at $0.

Offline, deterministic: pricing.batch_cost / realtime_cost are stubbed to fixed per-token rates so the arithmetic is
exact. plan_horizon is pure (no DB/LLM); horizon_report's live resolution is covered by the route_economics suite."""
import datetime
import os
import sys
import tempfile

os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="sg-horizon-")
os.environ.setdefault("SPENDGUARD_TEST_ISOLATED", "1")
os.environ.setdefault("SPENDGUARD_NO_AUTOINSTALL", "1")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from spendguard import route_horizon as rh, pricing  # noqa: E402

fails = []


def ck(name, cond, extra=""):
    print(("  [OK] " if cond else "  [FAIL] ") + name + (("  — " + extra) if extra and not cond else ""))
    if not cond:
        fails.append(name)


# Deterministic prices: batch $1/Mtok, realtime $4/Mtok (realtime > batch, as in reality). Stubbed on the pricing
# module both route_horizon and route_economics import, so _batch_cost / _realtime_per_tok are exact.
BATCH_RATE, RT_RATE = 1e-6, 4e-6
pricing.batch_cost = lambda model, in_tok, out_tok=0, **k: (int(in_tok) + int(out_tok)) * BATCH_RATE
pricing.realtime_cost = lambda model, in_tok, out_tok=0, **k: (int(in_tok) + int(out_tok)) * RT_RATE

NOW = datetime.datetime(2026, 10, 3, tzinfo=datetime.timezone.utc)
EFF_HI = 5e-6     # lane eff $5/Mtok (> batch → deferrable prefers batch)
EFF_LO = 0.5e-6   # lane eff $0.5/Mtok (< batch → deferrable uses lane-eff before batch)


def binding(eff, remaining_abs=100_000, used_abs=50_000, period_days=10, days_left=5):
    """A lane_economics binding bucket where _free_tokens resolves to a KNOWN ~free amount. With remaining 100k,
    used 50k over elapsed 5d (pace 10k/d) × 5d left = 50k projected → free = 50k; reserve 20% → budget 80k → eff tier 30k."""
    reset = NOW + datetime.timedelta(days=days_left)
    return {"remaining_abs": remaining_abs, "eff_usd_per_tok": eff, "used_abs": used_abs,
            "period_days": period_days, "reset_ts": reset.timestamp()}


RES = 0.2   # reserve fraction used throughout (reserve = 20k, lane_budget = 80k, free = 50k, eff tier = 30k)


def _alloc(res, intent):
    return next(a for a in res["allocations"] if a["intent"] == intent)


print("-- URGENT overflow → metered REALTIME (never batch); free+eff reclaimed first --")
rA = rh.plan_horizon([{"intent": "u", "n": 1, "in_tok": 0, "out_tok": 200_000, "urgency": "urgent"}],
                     lane_binding=binding(EFF_HI), lane_label="codex", batch_model="bm", realtime_model="rm",
                     reserve_frac=RES, now=NOW)
a = _alloc(rA, "u")
ck("urgent: 50k ~free lane tok at $0", abs(a["lane_free_tok"] - 50_000) < 1)
ck("urgent: 30k eff lane tok (fills the budget)", abs(a["lane_eff_tok"] - 30_000) < 1)
ck("urgent: 120k overflow → METERED REALTIME, not batch", abs(a["meter_tok"] - 120_000) < 1 and a["batch_tok"] == 0)
ck("urgent cost = 30k×$5/M + 120k×$4/M = $0.63", abs(a["usd"] - (30_000 * EFF_HI + 120_000 * RT_RATE)) < 1e-9, extra=str(a["usd"]))

print("-- DEFERRABLE overflow → cheap cap-free BATCH (never meter); eff skipped when batch undercuts it --")
rB = rh.plan_horizon([{"intent": "d", "n": 1, "in_tok": 0, "out_tok": 200_000, "urgency": "deferrable"}],
                     lane_binding=binding(EFF_HI), lane_label="codex", batch_model="bm", realtime_model="rm",
                     reserve_frac=RES, now=NOW)
b = _alloc(rB, "d")
ck("deferrable: 50k ~free reclaimed; eff SKIPPED (batch $1/M < eff $5/M)", abs(b["lane_free_tok"] - 50_000) < 1 and b["lane_eff_tok"] == 0)
ck("deferrable: 150k overflow → BATCH, not meter", abs(b["batch_tok"] - 150_000) < 1 and b["meter_tok"] == 0)
ck("deferrable cost = 150k×$1/M = $0.15", abs(b["usd"] - 150_000 * BATCH_RATE) < 1e-9, extra=str(b["usd"]))

print("-- DEFERRABLE uses lane-eff BEFORE batch when eff undercuts batch --")
rBl = rh.plan_horizon([{"intent": "d", "n": 1, "in_tok": 0, "out_tok": 200_000, "urgency": "deferrable"}],
                      lane_binding=binding(EFF_LO), lane_label="codex", batch_model="bm", realtime_model="rm",
                      reserve_frac=RES, now=NOW)
bl = _alloc(rBl, "d")
ck("deferrable w/ cheap lane: 50k free + 30k eff then 120k batch", abs(bl["lane_eff_tok"] - 30_000) < 1 and abs(bl["batch_tok"] - 120_000) < 1)

print("-- SHARED budget: urgent banks the ~free tokens BEFORE a deferrable group --")
rC = rh.plan_horizon([{"intent": "d", "n": 1, "in_tok": 0, "out_tok": 40_000, "urgency": "deferrable"},
                      {"intent": "u", "n": 1, "in_tok": 0, "out_tok": 40_000, "urgency": "urgent"}],
                     lane_binding=binding(EFF_HI), lane_label="codex", batch_model="bm", realtime_model="rm",
                     reserve_frac=RES, now=NOW)
cu, cd = _alloc(rC, "u"), _alloc(rC, "d")
ck("urgent group banks 40k of the 50k free pool first", abs(cu["lane_free_tok"] - 40_000) < 1)
ck("deferrable group gets only the remaining 10k free (not double-counted)", abs(cd["lane_free_tok"] - 10_000) < 1)
ck("deferrable remainder (30k) → batch", abs(cd["batch_tok"] - 30_000) < 1)

print("-- RESERVE held out: lane tokens used never exceed remaining − reserve (80k) --")
used_lane = sum(x["lane_free_tok"] + x["lane_eff_tok"] for x in rA["allocations"])
ck("lane tokens used (80k) ≤ remaining(100k) − reserve(20k)", used_lane <= 80_000 + 1)

print("-- HONEST DEGRADE: no converged lane → urgent→meter, deferrable→batch (no free pool invented) --")
rD = rh.plan_horizon([{"intent": "u", "n": 1, "in_tok": 0, "out_tok": 100_000, "urgency": "urgent"},
                      {"intent": "d", "n": 1, "in_tok": 0, "out_tok": 100_000, "urgency": "deferrable"}],
                     lane_binding=None, batch_model="bm", realtime_model="rm", reserve_frac=RES, converged=False, now=NOW)
du, dd = _alloc(rD, "u"), _alloc(rD, "d")
ck("no lane → urgent 100k all metered, $0 free", du["lane_free_tok"] == 0 and abs(du["meter_tok"] - 100_000) < 1)
ck("no lane → deferrable 100k all batch", abs(dd["batch_tok"] - 100_000) < 1 and dd["meter_tok"] == 0)

print("-- HONEST DEGRADE: no priced batch path for deferrable → UNPRICED (usd None), never $0 --")
rE = rh.plan_horizon([{"intent": "d", "n": 1, "in_tok": 0, "out_tok": 100_000, "urgency": "deferrable"}],
                     lane_binding=None, batch_model=None, realtime_model="rm", reserve_frac=RES, converged=False, now=NOW)
e = _alloc(rE, "d")
ck("unpriceable deferrable overflow → unpriced_tok=100k, usd None (not invented $0)",
   abs(e["unpriced_tok"] - 100_000) < 1 and e["usd"] is None and e["unpriced"])
ck("the set total is None when any group is unpriced (fail-closed for a budget gate)", rE["totals"]["usd"] is None)

print("-- urgency is a CLOSED enum: declared values parse, an UNKNOWN word RAISES (never a free-text meaning guess) --")
ck("'urgent' → URGENT", rh.normalize_urgency("urgent") == rh.URGENT)
ck("'deferrable' → DEFERRABLE", rh.normalize_urgency("deferrable") == rh.DEFERRABLE)
ck("None / '' → the declared default (deferrable)", rh.normalize_urgency(None) == rh.DEFERRABLE and rh.normalize_urgency("") == rh.DEFERRABLE)
_raised = False
try:
    rh.normalize_urgency("critical")       # the agentic_decisions failing input: must NOT be guessed as deferrable
except ValueError:
    _raised = True
ck("an unknown urgency ('critical') RAISES — spendguard never guesses what an arbitrary word means", _raised)

print(f"\n{'OK' if not fails else 'FAIL'} test_route_horizon: {len(fails)} failure(s)")
sys.exit(1 if fails else 0)

"""GUARD — route_economics: a subscription LANE is priced at its TRUE MARGINAL cost, never $0, and the batch-vs-lane-
vs-combo split is the true min-cost choice. Cost ARITHMETIC on measured inputs (no meaning decision).

Synthetic fixture: a claude-code-like token lane (remaining_abs 1M, eff $4.58/Mtok, 30-day window, half elapsed with
500K used → pace projects 500K more, so ~500K would EXPIRE UNUSED = free) vs the metered gpt-5-nano Batch API. Pins:
  (a) a lane arm is priced at eff_usd_per_tok, never $0, once PAST the free/waste tier;
  (b) the waste (free) tokens are credited ~$0;
  (c) the interactive RESERVE (reserve_frac of remaining_abs) is HELD OUT — bulk never spends it (lane placement caps
      at remaining_abs − reserve; the rest overflows);
  (d) route picks the LANE when the cap would otherwise be wasted (free capacity), BATCH when batch true-cost < lane
      marginal, and a COMBO at the crossover (free on lane + the rest to cheaper batch);
  plus honest degradation: an unconverged lane is NOT priced and the recommendation falls to batch.
Hermetic: pure arithmetic on the injected binding + real pricing.batch_cost; no network, no spend.
"""
import datetime
import os
import sys
import tempfile

os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="sg-routeecon-")
os.environ.setdefault("SPENDGUARD_TEST_ISOLATED", "1")
os.environ.setdefault("SPENDGUARD_NO_AUTOINSTALL", "1")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from spendguard import route_economics as re   # noqa: E402

fails = []


def ck(name, cond):
    print(("  [OK] " if cond else "  [FAIL] ") + name)
    if not cond:
        fails.append(name)


UTC = datetime.timezone.utc
NOW = datetime.datetime(2026, 1, 15, tzinfo=UTC)
EFF = 4.58e-06                                   # $/token = $4.58 / Mtok (claude lane, measured)
BINDING = {"remaining_abs": 1_000_000, "used_abs": 500_000, "period_days": 30, "eff_usd_per_tok": EFF,
           "reset_ts": (NOW + datetime.timedelta(days=15)).isoformat()}   # half elapsed, 500K used → ~500K free
BATCH = "gpt-5-nano"
RESERVE_FRAC = 0.2                               # 200K of the 1M held for interactive; bulk budget = 800K; free = 500K


def R(n):
    return re.route_cost("t", n, 500, 500, lane_binding=BINDING, lane_label="claude-code",
                         batch_model=BATCH, reserve_frac=RESERVE_FRAC, now=NOW)


# ── (b)+(d-lane): a job entirely within the free/waste tier → lane at ~$0, and it WINS over batch ──
print("-- (b)/(d) a job inside the free (waste) tier: lane at ~$0, wins over batch --")
r1 = R(400)                                      # V = 400K tokens <= free 500K
ck("(b) free/waste tokens credited ~$0: lane_only.usd == 0 for a job inside the free tier", r1["lane_only"]["usd"] == 0)
ck("(d) lane/combo WINS when the cap would otherwise be wasted (free capacity)",
   r1["recommend"]["path"] in ("lane_only", "combo") and r1["recommend"]["usd"] == 0)

# ── (a): a job into the eff tier → those tokens priced at eff_usd_per_tok, NEVER $0 ──
print("\n-- (a) past the free tier, lane tokens cost eff_usd_per_tok, never $0 --")
r2 = R(700)                                      # V = 700K = 500K free + 200K eff
ck("(a) a lane arm is priced at eff_usd_per_tok (not $0)", r2["lane_only"]["eff_usd_per_tok"] == EFF)
ck("(a) lane_only prices the 200K eff-tier tokens at eff (200K x eff), never $0",
   abs(r2["lane_only"]["usd"] - 200_000 * EFF) < 1e-9 and r2["lane_only"]["usd"] > 0)

# ── (d-combo): free on lane + eff-tier to the CHEAPER batch beats both lane-only and batch-only ──
print("\n-- (d) combo at the crossover: free on lane + rest to cheaper batch --")
ck("(d) recommend = combo, cheaper than lane-only AND batch-only",
   r2["recommend"]["path"] == "combo" and r2["combo"]["usd"] < r2["lane_only"]["usd"]
   and r2["combo"]["usd"] < r2["batch_only"]["usd"])
ck("(d) the eff-tier tokens went to batch (batch < lane eff), the free tokens stayed on the lane",
   r2["combo"]["lane_free_tokens"] == 500_000 and r2["combo"]["lane_eff_tokens"] == 0 and r2["combo"]["batch_tokens"] == 200_000)

# ── (c): a job past the bulk budget → the interactive reserve is HELD OUT ──
print("\n-- (c) the interactive reserve is respected: bulk never spends it --")
r3 = R(1000)                                     # V = 1M > lane_budget 800K
ck("(c) lane_only is INFEASIBLE past remaining_abs − reserve; overflow == the reserve-bounded remainder (200K)",
   r3["lane_only"]["feasible"] is False and r3["lane_only"]["reserve_tokens"] == 200_000
   and r3["lane_only"]["overflow_tokens"] == 200_000)
ck("(c) bulk lane placement caps at remaining_abs − reserve (800K); the 200K reserve is never spent",
   (r3["combo"]["lane_free_tokens"] + r3["combo"]["lane_eff_tokens"]) <= 800_000 + 1e-6)

# ── honest degradation: an unconverged lane is NOT priced; recommend falls to batch ──
print("\n-- honest degrade: an unconverged lane is not priced, recommend = batch --")
r_unconv = re.route_cost("t", 700, 500, 500, lane_binding=BINDING, lane_label="claude-code",
                         batch_model=BATCH, reserve_frac=RESERVE_FRAC, converged=False, now=NOW)
ck("unconverged lane → lane_only is None and recommend = batch_only (no invented lane rate)",
   r_unconv["lane_only"] is None and r_unconv["recommend"]["path"] == "batch_only")

print(f"\n{'[FAIL]' if fails else 'OK'} test_route_economics: {len(fails)} failure(s)")
sys.exit(1 if fails else 0)

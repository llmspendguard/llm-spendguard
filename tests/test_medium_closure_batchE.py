"""Batch-E logic/accuracy closure (line-by-line medium fixes):

  * anomaly.robust_z [9] — MAD==0 no longer flags a value that has a PRECEDENT in the baseline ([0,…,50] + a $10
    day is not anomalous), only a value above the largest already-seen.
  * callio.record [20/21] — returns None on a duplicate (batch, custom_id) instead of a fresh id (was inflating
    'added' counts).
  * equivalence._norm_json [35] — a top-level array is parsed as the array, not silently as its inner object.
  * equivalence.grade [36] — a rubric-judge FAILURE degrades to the text tier, never a fabricated 0.0 that would
    kill a variant.
  * gpu_port.cost_by_day [43] — a FUTURE-dated billed row is excluded (not booked into a day that hasn't happened).
  * coverage.audit [29] — an explicit roots=[] scans NOTHING (not the defaults).

Offline, isolated home.
"""
import os
import sys
import tempfile

if not os.environ.get("SPENDGUARD_TEST_ISOLATED"):
    os.environ["SPENDGUARD_TEST_ISOLATED"] = "1"
    os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="spendguard-medE-")
    os.execv(sys.executable, [sys.executable] + sys.argv)

from spendguard import anomaly, callio, equivalence, gpu_port, coverage   # noqa: E402

fails = 0


def ck(label, cond, extra=""):
    global fails
    if not cond:
        fails += 1
    print(f"  [{'OK' if cond else 'FAIL'}] {label}{('  — ' + extra) if extra and not cond else ''}")


# ── [9] anomaly: MAD==0 with an outlier is not a maximal anomaly for an in-range value ──────────────────────────
z_inrange, _ = anomaly.robust_z([0, 0, 0, 0, 0, 50], 10)     # 10 < the 50 already in history → has precedent
z_newhigh, _ = anomaly.robust_z([0, 0, 0, 0, 0, 50], 60)     # 60 > 50 → genuinely new extreme
z_flat_up, _ = anomaly.robust_z([5, 5, 5], 9)                # truly flat, a rise above → still flagged
ck("MAD==0 + in-range value → NOT flagged (has a precedent)", z_inrange == 0.0, f"z={z_inrange}")
ck("MAD==0 + new all-time high → flagged", z_newhigh == 99.0, f"z={z_newhigh}")
ck("truly-flat history + a rise → still flagged", z_flat_up == 99.0, f"z={z_flat_up}")

# ── [20/21] callio.record returns None on a duplicate ───────────────────────────────────────────────────────────
a = callio.record("i", "openai", "gpt", "batchX", "cid1", "p", "o")
b = callio.record("i", "openai", "gpt", "batchX", "cid1", "p", "o")   # same (batch, custom_id) → duplicate
ck("first record returns an id", bool(a))
ck("a duplicate (batch, custom_id) record returns None (not a fresh id)", b is None, f"got {b!r}")

# ── [35] equivalence._norm_json: top-level array is the array ────────────────────────────────────────────────────
ck("a top-level JSON array parses as the array, not its inner object",
   equivalence._norm_json('[1, {"a": 2}]') == [1, {"a": 2}], f"got {equivalence._norm_json('[1, {\"a\": 2}]')!r}")

# ── [36] equivalence.grade: a rubric-judge failure degrades to text, never a fabricated 0.0 ─────────────────────
import spendguard.adapters as _ad         # noqa: E402
_ad.call = lambda *a, **k: {"error": "judge down", "text": None}
sc, tier = equivalence.grade("hello world", "hello there", mode="rubric")   # not exact → reaches the rubric tier
ck("a failed rubric judge degrades to the text tier (not a 0.0 that kills the variant)",
   tier.startswith("text") and isinstance(sc, float) and sc > 0.0, f"got {(sc, tier)}")

# ── [43] gpu_port.cost_by_day: a future-dated billed row is excluded ────────────────────────────────────────────
now = 1_000_000.0
inwin = {"usd": 5.0, "start_ts": now - 3600, "end_ts": None}      # within window
future = {"usd": 9.0, "start_ts": now + 10_000, "end_ts": None}   # future-dated (ms/seconds bug) → excluded
by_day = gpu_port.cost_by_day([inwin, future], since=now - 86400, now=now)
ck("a future-dated billed row is NOT booked; the in-window one is", round(sum(by_day.values()), 2) == 5.0, f"got {by_day}")

# ── [29] coverage.audit(roots=[]) scans nothing ────────────────────────────────────────────────────────────────
ck("an explicit roots=[] scans nothing (not the defaults)", coverage.audit(roots=[]) == [])

print(f"\n{'[FAIL]' if fails else 'OK'} test_medium_closure_batchE: {fails} failure(s)")
sys.exit(1 if fails else 0)

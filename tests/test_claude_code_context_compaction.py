"""Phase-3 guard: context trajectory + compaction signal. A Claude Code conversation re-reads its whole context
every turn (cache_read dominates), so a session sustaining a large context is expensive to keep OPEN. This pins:

  1. context_trajectory: current/max/mean context (= in + cache_read + cache_write per turn) and the recurring
     re-read $/turn = last turn's cache_read × the model's cache-read rate.
  2. compaction_candidates: flags only conversations SUSTAINING context >= threshold over the last N turns (one big
     turn is not bloat; a short session is not bloat); ignores small-context sessions.
  3. measured_compaction_ratio: k is MEASURED from real context DROPS in the ledger (never assumed); savings =
     recurring $/turn × (1 − 1/k), and are shown only when k is measured.

Hermetic: isolated SPENDGUARD_HOME, seeded claude-code rows with known token splits, pricing monkeypatched. Zero spend."""
import os, sys, tempfile

if not os.environ.get("SPENDGUARD_TEST_ISOLATED"):
    home = tempfile.mkdtemp(prefix="spendguard-ccctx-")
    os.environ["SPENDGUARD_TEST_ISOLATED"] = "1"
    os.environ["SPENDGUARD_HOME"] = home
    os.execv(sys.executable, [sys.executable] + sys.argv)

from spendguard import claudecode, budget
import spendguard.pricing as _pr

fails = []
def ck(name, cond):
    print(("  [OK] " if cond else "  [FAIL] ") + name)
    if not cond:
        fails.append(name)

# cache-read rate = $1.00 / 1M tok → $1e-6 / tok, so recurring $/turn = cache_read × 1e-6
_pr.price = lambda m: {"cached_in": 1.0}

def seed(conv, mid, i, cr, cw, ts):
    budget._record_spend_event("anthropic", "claude-haiku-4-5", "est_chat", 0.01,
                               conv_id=conv, occurred_at=ts, in_tok=i, cache_read_tok=cr, cache_write_tok=cw,
                               source="claude-code", project="claude-code", dedup_key="cc:" + mid)

def ts(conv, n):
    return f"2026-08-24T{n:02d}:00:00+00:00"

# bigconv: 5 turns all at context 150,000 (in 10k + cache_read 140k) → SUSTAINED bloat, recurring = 140k×1e-6 = $0.14/turn
for n in range(1, 6):
    seed("bigconv", f"b{n}", 10000, 140000, 0, ts("bigconv", n))
# smallconv: 5 turns at context 5,000 → never flagged
for n in range(1, 6):
    seed("smallconv", f"s{n}", 1000, 4000, 0, ts("smallconv", n))
# shortconv: only 3 turns at 150k → above the size threshold but too FEW turns to be "sustained"
for n in range(1, 4):
    seed("shortconv", f"sh{n}", 10000, 140000, 0, ts("shortconv", n))
# two clean 10x context DROPS (compaction events) → measured k = 10
seed("drop1", "d1a", 0, 100000, 0, ts("drop1", 1)); seed("drop1", "d1b", 0, 10000, 0, ts("drop1", 2))
seed("drop2", "d2a", 0, 200000, 0, ts("drop2", 1)); seed("drop2", "d2b", 0, 20000, 0, ts("drop2", 2))

# 1. trajectory
t = claudecode.context_trajectory("bigconv")
ck("trajectory current/max = 150,000 context tokens", t["current"] == 150000 and t["max"] == 150000)
ck("trajectory mean = 150,000", round(t["mean"]) == 150000)
ck("recurring re-read $/turn = cache_read(140k) × $1e-6 = $0.14", round(t["recurring_read_usd_per_turn"], 4) == 0.14)

# 2. measured compaction ratio from the two 10x drops
k, k_n = claudecode.measured_compaction_ratio()
ck("measured k ≈ 10 from 2 observed context drops (never assumed)", round(k, 2) == 10.0 and k_n == 2)

# 3. candidates: bigconv flagged; smallconv + shortconv are not
cands, (k2, _n), stats = claudecode.compaction_candidates(min_context=100000, min_turns=5)
ids = {c["conv_id"] for c in cands}
ck("bigconv is a compaction candidate (sustained large context)", "bigconv" in ids)
ck("smallconv (small context) is NOT flagged", "smallconv" not in ids)
ck("shortconv (large but only 3 turns) is NOT flagged — one/few big turns is not sustained bloat", "shortconv" not in ids)
big = next(c for c in cands if c["conv_id"] == "bigconv")
ck("candidate carries recurring $/turn $0.14", round(big["recurring_read_usd_per_turn"], 4) == 0.14)
ck("savings = recurring × (1 − 1/k) = 0.14 × 0.9 = $0.126/turn (only because k is measured)",
   round(big["saved_usd_per_turn"], 4) == 0.126)
ck("scan is transparent — examined == flagged + below_threshold + too_few_turns (no silently dropped conversation)",
   stats["examined"] == stats["flagged"] + stats["below_threshold"] + stats["too_few_turns"])

print(("[OK]" if not fails else "[FAIL]") + " claude-code-context-compaction: %d failure(s)" % len(fails))
sys.exit(1 if fails else 0)

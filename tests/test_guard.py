"""guard.py — quantifying GUARDED spend (cache/block/cascade/advisor/plan) as a lognormal distribution via
cumulants that ADD over the independent sum (so per-(day,project,source) cumulant sums roll up to any scope).
Pure math + a local SQLite `savings` table. Offline, isolated home. Script-style."""
import os, sys, tempfile

if not os.environ.get("SPENDGUARD_TEST_ISOLATED"):
    os.environ["SPENDGUARD_TEST_ISOLATED"] = "1"
    os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="spendguard-guard-")
    os.execv(sys.executable, [sys.executable] + sys.argv)

from _script_checks import record_script_check
from spendguard import guard

fails = []

# ── _lognormal_cumulants: k1 = mean; k2 = variance = μ²(w−1), w = 1+cv²; degenerate inputs → zeros ──
k1, k2, k3, k4 = guard._lognormal_cumulants(100.0, 0.3)
record_script_check(fails, "cumulants: k1 == mean (μ)", abs(k1 - 100.0) < 1e-9)
record_script_check(fails, "cumulants: k2 == μ²·cv² for w=1+cv² (variance)", abs(k2 - (100.0**2 * (0.3**2))) < 1e-6)
record_script_check(fails, "cumulants: k3, k4 > 0 (right-skewed, heavy-tailed lognormal)", k3 > 0 and k4 > 0)
record_script_check(fails, "cumulants: μ<=0 → all zero (no negative/zero saving)", guard._lognormal_cumulants(0, 0.3) == (0.0, 0.0, 0.0, 0.0))
record_script_check(fails, "cumulants: cv=0 → variance 0, higher cumulants 0 (a point mass)", guard._lognormal_cumulants(50.0, 0.0) == (50.0, 0.0, 0.0, 0.0))

# ── record_saving: confidence → cv (cv = clamp(1−conf, .05, .9)); non-positive amounts are dropped; never raises ──
guard.record_saving("cache", 10.0, project="lmm")        # conf 0.95 → cv 0.05 (clamped floor)
guard.record_saving("advisor", 4.0, project="lmm")       # conf 0.50 → cv 0.50
guard.record_saving("block", 6.0, project="manga2anime") # conf 0.70 → cv 0.30
guard.record_saving("cache", 0.0, project="lmm")         # non-positive → dropped
guard.record_saving("cache", -5.0, project="lmm")        # negative → dropped
guard.record_saving("cache", "not-a-number", project="lmm")  # bad input → swallowed, never raises

db = guard._savings_db()
with guard.budget._lock:
    rows = db.execute("SELECT source, amount, cv, project FROM savings ORDER BY amount DESC").fetchall()
record_script_check(fails, "record_saving: only the 3 positive events recorded (0/neg/garbage dropped)", len(rows) == 3)
bysrc = {r[0]: r for r in rows}
record_script_check(fails, "record_saving: cache conf 0.95 → cv clamped to floor 0.05", abs(bysrc["cache"][2] - 0.05) < 1e-9)
record_script_check(fails, "record_saving: advisor conf 0.50 → cv 0.50", abs(bysrc["advisor"][2] - 0.50) < 1e-9)
record_script_check(fails, "record_saving: explicit confidence overrides the source default",
   (guard.record_saving("cache", 2.0, confidence=0.0, project="lmm") or True) and
   db.execute("SELECT cv FROM savings WHERE amount=2.0").fetchone()[0] == 0.9)  # 1−0 clamped to ceiling 0.9

# ── by_dims_guarded: per (day, project, source) event count + SUMMED cumulants (the additive server payload) ──
dims = guard.by_dims_guarded()
record_script_check(fails, "by_dims_guarded: one row per (day,project,source) present", len(dims) >= 3)
lmm_cache = next((d for d in dims if d["project"] == "lmm" and d["source"] == "cache"), None)
record_script_check(fails, "by_dims_guarded: rows carry n + k1..k4 cumulants", lmm_cache and "k1" in lmm_cache and lmm_cache["n"] >= 1)
record_script_check(fails, "by_dims_guarded: k1 (Σ means) is positive for a real saving", lmm_cache and lmm_cache["k1"] > 0)
# since-filter narrows by day
record_script_check(fails, "by_dims_guarded: a future `since` filters everything out", guard.by_dims_guarded(since="2999-01-01") == [])

print(("\n[FAIL] " if fails else "\n[OK] ") + f"guard: {len(fails)} failure(s)")
sys.exit(1 if fails else 0)

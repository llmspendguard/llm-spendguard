"""Batch-A accounting-truth closure (line-by-line medium fixes):

  * reconcile_anthropic.cost_by_day re-prices CACHE-AWARE — a stored cache-read/creation token breakdown is
    priced at its own rates, not dropped (the old in/out-only reprice silently discarded all cache-token spend).
    A legacy record (no breakdown) still reprices from in/out — no regression.
  * codex.update PRUNES cached digests whose session file was deleted (they used to bill forever).

Offline, isolated home; network refresh + state save are stubbed.
"""
import os
import sys
import tempfile
import json

if not os.environ.get("SPENDGUARD_TEST_ISOLATED"):
    os.environ["SPENDGUARD_TEST_ISOLATED"] = "1"
    os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="spendguard-medA-")
    os.execv(sys.executable, [sys.executable] + sys.argv)

from spendguard import reconcile_anthropic as ra, pricing, codex   # noqa: E402

fails = 0


def ck(label, cond, extra=""):
    global fails
    if not cond:
        fails += 1
    print(f"  [{'OK' if cond else 'FAIL'}] {label}{('  — ' + extra) if extra and not cond else ''}")


os.makedirs(os.path.dirname(ra.CACHE_PATH), exist_ok=True)
ra.refresh_cache = lambda k, c: 0          # don't fetch; price only what's seeded
ra._key = lambda: "x"
M = "claude-opus-4-8"

# ── reconcile_anthropic: cache tokens are re-priced, not dropped ────────────────────────────────────────────────
cache = {"b1": {"created_at": "2026-06-10", "cost": 0.0,
                "by_model": {M: {"in": 1_000_000, "out": 0, "cread": 1_000_000, "ccreate": 1_000_000, "cost": 0.0}}}}
with open(ra.CACHE_PATH, "w") as f:
    json.dump(cache, f)
by_day, _ = ra.cost_by_day()
exp = ra._price_tokens(M, 1_000_000, 1_000_000, 1_000_000, 0)
inout_only = ra._price_tokens(M, 1_000_000, 0, 0, 0)
ck("cache-read + cache-creation tokens are priced (not dropped)",
   abs(by_day.get("2026-06-10", 0) - exp) < 1e-9 and exp > 0, f"got {by_day} exp {exp}")
ck("cache-aware reprice STRICTLY exceeds an in/out-only reprice (cache tokens count)", exp > inout_only + 1e-9)

# a LEGACY record (no cread/ccreate) still reprices from in/out — no regression, matches batch_cost
cache2 = {"b2": {"created_at": "2026-06-11", "cost": 0.0, "by_model": {M: {"in": 1_000_000, "out": 0, "cost": 0.0}}}}
with open(ra.CACHE_PATH, "w") as f:
    json.dump(cache2, f)
by_day2, _ = ra.cost_by_day()
ck("legacy in/out-only record reprices from in/out (== batch_cost)",
   abs(by_day2.get("2026-06-11", 0) - pricing.batch_cost(M, 1_000_000, 0)) < 1e-9)

# ── codex.update prunes a deleted session ───────────────────────────────────────────────────────────────────────
d = tempfile.mkdtemp(prefix="codexsess-")          # an EXISTING but empty sessions dir → prune activates
codex._sessions_dir = lambda: d
codex._save_state = lambda s: None                 # don't touch disk
gone = os.path.join(d, "gone.jsonl")
st = {"sessions": {gone: {"mtime": 1.0, "digest": {"cost": 5.0}}}}
st2, _changed = codex.update(st)
ck("a deleted session's cached digest is pruned from state", gone not in st2["sessions"])

print(f"\n{'[FAIL]' if fails else 'OK'} test_medium_closure_batchA: {fails} failure(s)")
sys.exit(1 if fails else 0)

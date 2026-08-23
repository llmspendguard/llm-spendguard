"""record_realtime_reconstruction folds the agentic realtime cache into the ledger at EACH run's OWN day — so a
multi-month reconstruction lands in the right months (the fix for the current-month gap a single lump-day fold left
open). Guards: per-day dating (two runs, same org+provider, DIFFERENT days → separate rows at their own days), a
row with no day falls back to the window start, and a non-positive row is COUNTED (skipped), never silently dropped.
Offline: budget.record_reconciled/clear_reconciled mocked, isolated home, no real ledger writes.
"""
import json
import os
import sys
import tempfile

os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="sg-rtrecon-")
os.environ.setdefault("SPENDGUARD_TEST_ISOLATED", "1")
os.environ.setdefault("SPENDGUARD_NO_AUTOINSTALL", "1")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from spendguard import ledger_sync, budget, config                                    # noqa: E402


def ck(name, cond):
    ok = bool(cond)
    print(("  [OK] " if ok else "  [FAIL] ") + name)
    return [] if ok else [name]


fails = []
# a reconstruction cache with per-run days across TWO months + a no-day row + a non-positive row
cache = {"since": "2026-05-01", "rows": [
    {"org": "healiom", "provider": "anthropic", "day": "2026-08-05", "usd": 10.0},
    {"org": "healiom", "provider": "anthropic", "day": "2026-08-20", "usd": 3.0},    # same org+prov, DIFFERENT day
    {"org": "ensight", "provider": "openai", "day": "2026-06-12", "usd": 5.0},
    {"org": "healiom", "provider": "anthropic", "usd": 2.0},                          # NO day → fallback to window start
    {"org": "ensight", "provider": "openai", "day": "2026-07-01", "usd": 0.0},        # non-positive → skipped, not dropped
]}
with open(str(config.HOME / "realtime_reconstruction.json"), "w") as f:
    f.write(json.dumps(cache))

recorded = []
budget.clear_reconciled = lambda **k: None
budget.record_reconciled = lambda day, provider, cost, project, kind, model: recorded.append(
    {"day": day, "provider": provider, "cost": cost, "project": project})

res = ledger_sync.record_realtime_reconstruction()
days = {r["day"] for r in recorded}

print("-- per-run day dating --")
fails += ck("each run recorded at its OWN day (Aug 5, Aug 20, Jun 12 all present — not one lump day)",
            {"2026-08-05", "2026-08-20", "2026-06-12"} <= days)
aug = [r for r in recorded if r["provider"] == "anthropic" and r["day"] in ("2026-08-05", "2026-08-20")]
fails += ck("two same-org+provider runs on different days stay SEPARATE rows (Aug 5 vs Aug 20)", len(aug) == 2)

print("\n-- fallback + skip --")
fb = str(cache["since"])[:10]
fails += ck("a row with no day falls back to the window start (cache 'since')",
            any(r["day"] == fb and abs(r["cost"] - 2.0) < 0.01 for r in recorded))
fails += ck("non-positive row is COUNTED as skipped (never silently dropped)", res.get("skipped") == 1)
fails += ck("recorded total = the four positive rows (10+3+2+5=20), zero row excluded",
            abs(res.get("recorded", 0) - 20.0) < 0.01)

print("\n-- conv.session_day_map: earliest day per session (the fallback that dates a run resolve couldn't) --")
from spendguard import conv                                                            # noqa: E402
segs = [
    {"sid": "s1", "day": "2026-08-10"},
    {"sid": "s1", "day": "2026-08-03"},                   # earlier → wins
    {"sid": "s2", "day": "2026-06-15"},
    {"sid": "s3"},                                        # no day → omitted
    {"day": "2026-07-01"},                                # no sid → omitted
]
m = conv.session_day_map(segs)
fails += ck("earliest day per session (s1→Aug 3, s2→Jun 15)", m.get("s1") == "2026-08-03" and m.get("s2") == "2026-06-15")
fails += ck("a session with no day is omitted (no bogus entry)", "s3" not in m and None not in m)

print(f"\n{'[FAIL]' if fails else 'OK'} test_realtime_recon_dating: {len(fails)} failure(s)")
sys.exit(1 if fails else 0)

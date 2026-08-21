"""Run N bake-offs for ONE intent, each on a DIFFERENT real stored prompt sample, to gather VARIED evidence about
which lane wins for it — the check to make before allow-listing a quality-sensitive intent (one bake-off is thin).
Gated, fail-closed.

    .venv.nosync/bin/python scripts/lane/bakeoff_intent_samples.py <intent> [N=3]
"""
import sys
import sqlite3
import contextlib

sys.path.insert(0, "src")
import spendguard                                                   # noqa: E402
spendguard.require()                                               # FAIL CLOSED
from spendguard import config, lane_bandit                          # noqa: E402

intent = sys.argv[1] if len(sys.argv) > 1 else None
n = int(sys.argv[2]) if len(sys.argv) > 2 else 3
if not intent:
    print("usage: bakeoff_intent_samples.py <intent> [N]")
    raise SystemExit(2)

with contextlib.closing(sqlite3.connect(str(config.db_path()))) as c:
    rows = c.execute("SELECT prompt_snip FROM calls WHERE intent=? AND prompt_snip IS NOT NULL "
                     "AND length(prompt_snip) > 20 GROUP BY prompt_snip ORDER BY MAX(ts) DESC LIMIT ?",
                     (intent, n)).fetchall()

print(f"{len(rows)} distinct sample(s) for {intent} — one bake-off each:\n")
wins, errors = {}, 0
for (task,) in rows:
    try:
        res = lane_bandit.run_bakeoff(intent, task)
    except Exception as e:
        # I1: a discarded unit of work leaves a trace — name the sample that failed + count it, never a silent drop.
        errors += 1
        print(f"  bake-off ERROR on sample [{task[:46]}…]: {str(e)[:80]} — skipped ({errors} failed so far)")
        continue
    if res and res.get("text"):
        wins[res["lane"]] = wins.get(res["lane"], 0) + 1
        print(f"  → {res['lane']:<8} · {res.get('use_name'):<24} ({res.get('why')})   [{task[:46]}…]")
    else:
        print(f"  → no winner   [{task[:46]}…]")

print(f"\nwins across {len(rows)} varied samples: {wins or '(none)'}"
      + (f"  ({errors} errored)" if errors else ""))
print("\nlearned table for this intent:")
for arm, s in sorted(lane_bandit.arm_stats(intent).items(), key=lambda kv: -kv[1].get("winrate", 0)):
    print(f"  {arm[0]:<8} {arm[1]:<24} winrate {s.get('winrate', 0):.2f}  ({s.get('trials', 0):.1f} trials)")

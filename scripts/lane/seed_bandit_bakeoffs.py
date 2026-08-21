"""Seed the lane bandit's learned table with ONE bake-off per named intent, using each intent's REAL stored prompt
sample (calls.prompt_snip) so the initial winner reflects the actual work — not a synthetic task. Run UNDER THE GATE
(the .venv.nosync interpreter). Cost: one cheap judge call per bake-off (~$0.004); the two lane answers are $0,
plan-served (a tie short-circuits the judge, so it can be less).

    .venv.nosync/bin/python scripts/lane/seed_bandit_bakeoffs.py <intent> [<intent> ...]

NB the stored sample is a PREFIX (calls.snippet_len, default 200 chars), so this seed reflects the task SHAPE; the
main path then refines each intent on its full prompts via equal-start + ε bake-offs.
"""
import sys
import sqlite3
import contextlib

sys.path.insert(0, "src")
import spendguard                                                   # noqa: E402
spendguard.require()                                               # FAIL CLOSED: the gate must actually be enforcing here
from spendguard import config, lane_bandit                          # noqa: E402

intents = sys.argv[1:]
if not intents:
    print("usage: seed_bandit_bakeoffs.py <intent> [<intent> ...]")
    raise SystemExit(2)

print(f"Seeding {len(intents)} intent(s) with one real-sample bake-off each (2 lanes $0 + a cheap judge):\n")
with contextlib.closing(sqlite3.connect(str(config.db_path()))) as c:
    for intent in intents:
        r = c.execute("SELECT prompt_snip FROM calls WHERE intent=? AND prompt_snip IS NOT NULL "
                      "AND length(prompt_snip) > 20 ORDER BY ts DESC LIMIT 1", (intent,)).fetchone()
        task = r[0] if r else None
        if not task:
            print(f"  {intent:<34} — no stored prompt sample, skipped")
            continue
        try:
            res = lane_bandit.run_bakeoff(intent, task)
        except Exception as e:
            print(f"  {intent:<34} — bake-off error: {str(e)[:80]}")
            continue
        if res and res.get("text"):
            print(f"  {intent:<34} → {res['lane']} · {res.get('use_name')}  ({res.get('why')})")
        else:
            print(f"  {intent:<34} → no winner (need ≥2 live lanes with configured models)")

print("\nLearned table now:")
lane_bandit.main()

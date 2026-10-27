"""Guard: the status-line COMPACTION NUDGE. spendguard surfaces, in the current session's status line, when that
session is expensive to keep alive — it tails the session's transcript for the last turn's re-read context and, if
that is at/above the configured threshold, appends a '/compact' suggestion. The one-liner makes NO savings claim —
the old '~1x cheaper' asserted a saving at identical cost (and whether a ratio is a saving worth showing is not a
cutoff a per-turn hook may hard-code); the tok/turn count is the signal. Pins:
  1. a bloated session (context >= threshold) → a nudge with the tok/turn + '/compact', and NO '~Nx cheaper' claim.
  2. a small session → no nudge; no transcript → no nudge (graceful, never raises).
  3. it reads the LAST turn (so once a session compacts and drops below threshold, the nudge disappears).
Hermetic: isolated home + fabricated transcript + hint. Zero spend, no import of the heavy path."""
import os, sys, tempfile, json

if not os.environ.get("SPENDGUARD_TEST_ISOLATED"):
    home = tempfile.mkdtemp(prefix="spendguard-nudge-")
    os.environ["SPENDGUARD_TEST_ISOLATED"] = "1"
    os.environ["SPENDGUARD_HOME"] = home
    os.execv(sys.executable, [sys.executable] + sys.argv)

from _script_checks import record_script_check
from spendguard import receipt

fails = []
HOME = os.environ["SPENDGUARD_HOME"]
with open(os.path.join(HOME, "compaction_hint.json"), "w") as f:
    json.dump({"threshold_tokens": 100000, "k": 11.2}, f)

def turn(i, cr, cw, o):
    return json.dumps({"message": {"usage": {"input_tokens": i, "cache_read_input_tokens": cr,
                                             "cache_creation_input_tokens": cw, "output_tokens": o}}})

big = os.path.join(HOME, "big.jsonl")
with open(big, "w") as f:
    f.write(turn(10000, 490000, 0, 100) + "\n")            # context = 500,000 >> threshold
small = os.path.join(HOME, "small.jsonl")
with open(small, "w") as f:
    f.write(turn(1000, 4000, 0, 50) + "\n")                # context = 5,000 << threshold

n_big = receipt._compaction_nudge({"transcript_path": big}, HOME)
record_script_check(fails, "bloated session → the EXACT claim-free one-liner (tok/turn + /compact, no savings multiplier)",
   n_big == "⚠ 500K tok/turn · /compact (guided)")
record_script_check(fails, "nudge signals it is (guided) — so the user knows the hook will preserve context", "(guided)" in n_big)
record_script_check(fails, "small session → no nudge", receipt._compaction_nudge({"transcript_path": small}, HOME) == "")
record_script_check(fails, "no transcript → no nudge (graceful)", receipt._compaction_nudge({}, HOME) == "")
record_script_check(fails, "missing transcript file → no nudge", receipt._compaction_nudge({"transcript_path": "/no/such.jsonl"}, HOME) == "")

# it reads the LAST turn: after a compaction, the newest turn is small → the nudge disappears
with open(big, "a") as f:
    f.write(turn(1000, 2000, 0, 50) + "\n")                # a post-compaction small turn is now the last
record_script_check(fails, "reads the LAST turn — once compacted (below threshold) the nudge is gone",
   receipt._compaction_nudge({"transcript_path": big}, HOME) == "")

print(("[OK]" if not fails else "[FAIL]") + " statusline-compaction-nudge: %d failure(s)" % len(fails))
sys.exit(1 if fails else 0)

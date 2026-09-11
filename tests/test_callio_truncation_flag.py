"""Guard — call_io FLAGS a truncated capture and EXCLUDES it from replay (the 800-char silent-truncation fail).

A prompt CUT at the capture cap is a different task; a bakeoff/titration replaying it measures something other than
the work it claims to. So capture records a `truncated` flag, and the replay source (bakeoff._sample_prompts) returns
only FULL-fidelity bodies. Pins:
  · a full body (≤ cap) → truncated=0, returned by _sample_prompts;
  · a body LONGER than the cap → truncated=1, NOT returned (excluded from replay — never silently a partial task);
  · a bigger-cap re-fetch of the full body GROWS the row + CLEARS the flag → replayable again ($0 grow-only recovery);
  · an all-truncated intent yields [] → the caller refuses cleanly, rather than replaying a partial prompt.
Offline, no spend (record_io_sample is a local DB write; snip_chars is monkeypatched to a small cap)."""
import os, sys, tempfile
if not os.environ.get("SPENDGUARD_TEST_ISOLATED"):
    os.environ["SPENDGUARD_TEST_ISOLATED"] = "1"
    os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="spendguard-trunc-")
    _self = os.path.realpath(__file__)
    _root = os.path.realpath(os.path.dirname(__file__)) + os.sep
    if not _self.startswith(_root):
        raise SystemExit("refusing to re-exec a path outside the test directory: %s" % _self)
    os.execv(sys.executable, [sys.executable, _self])

from spendguard import callio, bakeoff

_fails = []
def ck(name, cond):
    print(("  [OK] " if cond else "  [FAIL] ") + name)
    if not cond:
        _fails.append(name)

callio.snip_chars = lambda: 100          # small cap so a "long" body is easy to make (no huge strings)
FULL = "x" * 80                          # ≤ cap → complete
LONG = "y" * 400                         # > cap → will be cut → truncated

callio.record_io_sample("t_full", "openai", "m", "b1", "c1", FULL, "out", source="test")
callio.record_io_sample("t_cut", "openai", "m", "b2", "c2", LONG, "out", source="test")

con = callio._callio_db()
tf = con.execute("SELECT truncated FROM call_io WHERE intent='t_full'").fetchone()[0]
tc = con.execute("SELECT truncated, LENGTH(prompt) FROM call_io WHERE intent='t_cut'").fetchone()
ck("a full body is flagged truncated=0", tf == 0)
ck("a CUT body is flagged truncated=1 (and stored at the cap)", tc[0] == 1 and tc[1] == 100)

print("-- the replay source returns FULL bodies, EXCLUDES truncated ones --")
ck("full body IS returned for replay", FULL in bakeoff._sample_prompts("t_full", 10))
ck("cut body is EXCLUDED from replay (no silent partial task)", bakeoff._sample_prompts("t_cut", 10) == [])

print("-- a bigger-cap re-fetch recovers the full body + clears the flag (grow-only) --")
callio.snip_chars = lambda: 1000
callio.record_io_sample("t_cut", "openai", "m", "b2", "c2", LONG, "out", source="test")   # same key → grows in place
rec = con.execute("SELECT truncated, LENGTH(prompt) FROM call_io WHERE intent='t_cut'").fetchone()
ck("re-fetch at a larger cap CLEARS truncated + stores the full body", rec[0] == 0 and rec[1] == 400)
ck("the recovered body is replayable again", LONG in bakeoff._sample_prompts("t_cut", 10))

print("-- an OUTPUT-only cut keeps a FULL prompt replayable (the output regenerates; the prompt is intact) --")
callio.snip_chars = lambda: 100
callio.record_io_sample("t_outcut", "openai", "m", "b3", "c3", "p" * 50, "o" * 400, source="test")  # prompt full, output cut
oc = con.execute("SELECT truncated FROM call_io WHERE intent='t_outcut'").fetchone()[0]
ck("full prompt + cut output → truncated=0 (prompt is not conflated with output)", oc == 0)
ck("an output-cut row is still returned for replay", ("p" * 50) in bakeoff._sample_prompts("t_outcut", 10))

print("-- the code DEFAULT cap is documented as judge-sized, not replay-sized (enforcement, not prose) --")
ck("_IO_SNIP_DEFAULT exists (the default the replay guard protects against)", isinstance(callio._IO_SNIP_DEFAULT, int))

print(("[OK]" if not _fails else "[FAIL]") + " callio truncation flag: %d failure(s)" % len(_fails))
sys.exit(1 if _fails else 0)

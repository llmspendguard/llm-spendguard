"""Guard — call_io never truncates retained evidence or sets a truncation flag.

Compression replaces the historic character cap: bodies below and above the configured legacy cap must both
round-trip whole and remain replayable. The schema flag remains for legacy-row compatibility only.
Offline, no spend (record_io_sample is a local DB write)."""
import os, sys, tempfile
if not os.environ.get("SPENDGUARD_TEST_ISOLATED"):
    os.environ["SPENDGUARD_TEST_ISOLATED"] = "1"
    os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="spendguard-trunc-")
    _self = os.path.realpath(__file__)
    _root = os.path.realpath(os.path.dirname(__file__)) + os.sep
    if not _self.startswith(_root):
        raise SystemExit("refusing to re-exec a path outside the test directory: %s" % _self)
    os.execv(sys.executable, [sys.executable, _self])

from _script_checks import record_script_check  # noqa: E402

from spendguard import callio, bakeoff

_fails = []
callio.snip_chars = lambda: 100          # small cap so a "long" body is easy to make (no huge strings)
FULL = "x" * 80                          # ≤ cap → complete
LONG = "y" * 400                         # > legacy cap, but must remain whole

callio.record_io_sample("t_full", "openai", "m", "b1", "c1", FULL, "out", source="test")
callio.record_io_sample("t_cut", "openai", "m", "b2", "c2", LONG, "out", source="test")

import contextlib
with contextlib.closing(callio.config.fresh_ledger_conn(callio._ensure_callio_schema)) as con:
    tf = con.execute("SELECT truncated FROM call_io WHERE intent='t_full'").fetchone()[0]
    tc = con.execute("SELECT truncated, prompt FROM call_io WHERE intent='t_cut'").fetchone()
    record_script_check(_fails, "a full body is flagged truncated=0", tf == 0)
    record_script_check(_fails, "a body above the legacy cap stays whole with truncated=0", tc[0] == 0 and tc[1] == LONG)

    print("-- the replay source returns every whole body --")
    record_script_check(_fails, "full body IS returned for replay", FULL in bakeoff._sample_prompts("t_full", 10))
    record_script_check(_fails, "long body remains replayable", LONG in bakeoff._sample_prompts("t_cut", 10))

    print("-- a bigger-cap duplicate remains idempotent and whole --")
    callio.snip_chars = lambda: 1000
    callio.record_io_sample("t_cut", "openai", "m", "b2", "c2", LONG, "out", source="test")   # same key → grows in place
    rec = con.execute("SELECT truncated, prompt FROM call_io WHERE intent='t_cut'").fetchone()
    record_script_check(_fails, "duplicate keeps truncated=0 and the full body", rec[0] == 0 and rec[1] == LONG)
    record_script_check(_fails, "the recovered body is replayable again", LONG in bakeoff._sample_prompts("t_cut", 10))

    print("-- an OUTPUT-only cut keeps a FULL prompt replayable (the output regenerates; the prompt is intact) --")
    callio.snip_chars = lambda: 100
    callio.record_io_sample("t_outcut", "openai", "m", "b3", "c3", "p" * 50, "o" * 400, source="test")  # prompt full, output cut
    oc = con.execute("SELECT truncated FROM call_io WHERE intent='t_outcut'").fetchone()[0]
    record_script_check(_fails, "full prompt + cut output → truncated=0 (prompt is not conflated with output)", oc == 0)
    record_script_check(_fails, "an output-cut row is still returned for replay", ("p" * 50) in bakeoff._sample_prompts("t_outcut", 10))

    print("-- the code DEFAULT cap is documented as judge-sized, not replay-sized (enforcement, not prose) --")
    record_script_check(_fails, "_IO_SNIP_DEFAULT exists (the default the replay guard protects against)", isinstance(callio._IO_SNIP_DEFAULT, int))

    print(("[OK]" if not _fails else "[FAIL]") + " callio truncation flag: %d failure(s)" % len(_fails))
    sys.exit(1 if _fails else 0)

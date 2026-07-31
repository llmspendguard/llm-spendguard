"""Three deferred findings, fixed together.

1. **UTC/local month-boundary drift.** Every ledger day-key is written in UTC, but all 15 default `since`
   windows were built from `date.today()` — LOCAL. West of UTC the month boundary is wrong for 7-8 hours around
   the 1st, so `trust` / `close` / the leak check computed a residual that changed with the time of day and then
   self-corrected — the hardest possible bug to trust-debug. One helper (`config.month_start_utc`), used
   everywhere.
2. **Unpriced units estimated at $0 SILENTLY in the pre-spend path.** `_est_usd_images` / `_est_usd_speech`
   returned $0 on KeyError with no warning while their `_act_*` twins warned. That value feeds the CAP check, so
   an unpriced model could never trip a cap and the user learned only after the money was gone — backwards for a
   pre-spend gate.
3. **`--help` / `--version` didn't exist.** The module docstring (10 of 60+ commands) printed for every help
   request, every version request AND every typo, always exiting 1.
"""
import os, sys, tempfile, datetime

if not os.environ.get("SPENDGUARD_TEST_ISOLATED"):
    os.environ["SPENDGUARD_TEST_ISOLATED"] = "1"
    os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="spendguard-deferred-")
    os.execv(sys.executable, [sys.executable] + sys.argv)

import io
import contextlib
import inspect
from spendguard import config, cli, gate

failures = 0
def check(label, cond):
    global failures
    ok = bool(cond)
    if not ok: failures += 1
    print(f"  [{'OK' if ok else 'FAIL'}] {label}")


print("-- 1. money windows are UTC, matching how day-keys are WRITTEN --")
utc_now = datetime.datetime.now(datetime.timezone.utc)
check("month_start_utc is the 1st of the UTC month", config.month_start_utc() == utc_now.strftime("%Y-%m-01"))
check("today_utc is the UTC date", config.today_utc() == utc_now.strftime("%Y-%m-%d"))
check("both are plain YYYY-MM-DD strings (what the ledger compares against)",
      len(config.month_start_utc()) == 10 and config.month_start_utc().endswith("-01"))
# the real regression: NO module may still derive a default window from local time
import pathlib
src_dir = pathlib.Path(config.__file__).parent
offenders = []
for p in src_dir.glob("*.py"):
    for i, ln in enumerate(p.read_text().splitlines(), 1):
        if "date.today().replace(day=1)" in ln:
            offenders.append(f"{p.name}:{i}")
check(f"no module builds a month window from LOCAL time: {offenders}", not offenders)
for mod in ("trust", "ledger_sync", "report", "saas", "signal", "workdone", "cli"):
    m = __import__(f"spendguard.{mod}", fromlist=["x"])
    check(f"{mod} uses the shared UTC helper", "month_start_utc" in inspect.getsource(m))

print("-- 2. an unpriced unit is LOUD in the pre-spend path (it feeds the cap check) --")
gate._unit_warned.clear()
buf = io.StringIO()
with contextlib.redirect_stderr(buf):
    model, usd = gate._est_usd_images({"model": "no-such-image-model-xyz", "n": 2})
check("estimate still returns $0 (never a guessed price)", usd == 0.0)
check("but it WARNS, naming the model and the fix",
      "no image unit price" in buf.getvalue() and "no-such-image-model-xyz" in buf.getvalue()
      and "sync-prices" in buf.getvalue())
gate._unit_warned.clear()
buf = io.StringIO()
with contextlib.redirect_stderr(buf):
    _m, usd2 = gate._est_usd_speech({"model": "no-such-tts-model-xyz", "input": "hello"})
check("tts estimate warns too", usd2 == 0.0 and "tts_char" in buf.getvalue())
buf = io.StringIO()                                  # dedup: one warning per (kind, model), not per call
with contextlib.redirect_stderr(buf):
    gate._est_usd_speech({"model": "no-such-tts-model-xyz", "input": "hello"})
check("the warning is deduped per (kind, model) — not spam in a loop", buf.getvalue() == "")
gate._unit_warned.clear()
buf = io.StringIO()
from spendguard import pricing
pricing.UNIT_PRICES["image"]["test-priced-model"] = 0.04   # inject: the synced cache is absent in an isolated home
with contextlib.redirect_stderr(buf):
    _m, usd3 = gate._est_usd_images({"model": "test-priced-model", "n": 2})
check("a PRICED model estimates silently and correctly ($0.08 for 2)",
      abs(usd3 - 0.08) < 1e-9 and "WARN" not in buf.getvalue())

print("-- 3. --help / --version exist, exit 0, and cover the real surface --")
def run(args):
    b = io.StringIO()
    with contextlib.redirect_stdout(b):
        rc = cli.main(args)
    return rc, b.getvalue()

for flag in ("--help", "-h", "help"):
    rc, out = run([flag])
    check(f"`{flag}` exits 0 with the grouped surface", rc == 0 and "start here:" in out and "see the money:" in out)
rc, out = run(["--version"])
from spendguard import __version__
check("`--version` prints the real version and exits 0", rc == 0 and __version__ in out)
check("`-V` works too", run(["-V"])[0] == 0)
rc, out = run(["--commands"])
check("help lists many more than the old 10", rc == 0 and len(cli._all_commands()) >= 25)
check("the four first-run commands are in 'start here'",
      all(c in cli.help_text() for c in ("scan", "run", "init", "doctor")))
check("it points at the full reference rather than pretending to be complete",
      "docs.llmspendguard.com" in cli.help_text() and "Not listed here" in cli.help_text())

print("-- help can't drift from the dispatch: every advertised command must dispatch --")
disp = inspect.getsource(cli._dispatch)
missing = [c for c in cli._all_commands() if f'"{c}"' not in disp]
check(f"every command in the help table is really dispatched: {missing}", not missing)

print("-- a typo gets did-you-mean and exit 2 (not the old exit-1 docstring dump) --")
err = io.StringIO()
with contextlib.redirect_stderr(err):
    rc = cli.main(["reconcil"])
check("exit 2 with a suggestion", rc == 2 and "did you mean" in err.getvalue() and "reconcile" in err.getvalue())

print(f"\n{'[FAIL]' if failures else 'OK'} test_deferred_fixes: {failures} failure(s)")
sys.exit(1 if failures else 0)

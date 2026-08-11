"""No test may write to a real user path. Enforced, because prose did not hold.

WHAT HAPPENED. A guard added on 2026-08-10 to prove the config cache re-reads a changed file did this:

    _cfg.CONFIG_JSON.write_text(json.dumps({"probe": {"k": "first"}}))

`_cfg.CONFIG_JSON` is ~/.spendguard/config.json, and the test that ran it shares the real HOME. It
OVERWROTE the user's settings — the file went from ~9KB to 26 bytes — and every knob in it was lost.
`calls.enabled` silently became False, so the next review wave recorded nothing to the call log, and that
wave's independent ledger cross-check read $0.00. A guard that damages the thing it guards is worse than
no guard, and this one was invisible: the suite stayed green throughout.

WORSE: there was no backup. ~/.spendguard holds the spend ledger too — 43,488 rows and $24,515 of
recorded spend at the time — and the machine's B2 job mirrors ~/.claude only. The standing rule here is
integrity before any mutation; a mutation with no recovery path is not a change, it is a gamble.

WHAT THIS ENFORCES. Every test script either:
  * re-execs itself into a temp SPENDGUARD_HOME before importing anything (the pattern most already use), or
  * appears in READ_ONLY below, meaning it was read and confirmed to write nothing outside a temp dir.

The list is deliberately explicit. A new test that touches real state has to be argued for in this file
rather than discovered later by the damage.
"""
import pathlib
import sys

HERE = pathlib.Path(__file__).parent
ISOLATION = 'os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp'

# Tests confirmed by reading to write nothing outside a temp directory: pure functions, source inspection,
# in-memory fixtures, or their own tempfile.mkdtemp. Adding a name here is a claim you have checked it.
READ_ONLY = {
    "test_runner.py", "test_no_test_touches_real_user_data.py",
}

failures = 0


def check(label, cond, extra=""):
    global failures
    if not cond:
        failures += 1
    print(f"  [{'OK' if cond else 'FAIL'}] {label}{('  — ' + extra) if extra and not cond else ''}")


# The writes that reach real user state. These are attribute/callable references, not arbitrary paths:
# a test naming one of them without isolation is writing where the user's data lives.
REAL_PATHS = ("CONFIG_JSON", "saas_path()", "user_prices_path()", "db_path()", "config.HOME")
WRITES = (".write_text(", ".write_bytes(", "open(", "os.remove(", "os.replace(", "shutil.")

unisolated = []
for f in sorted(HERE.glob("test_*.py")):
    if f.name in READ_ONLY:
        continue
    src = f.read_text()
    if ISOLATION in src:
        continue                       # re-execs into a temp HOME: cannot reach real state
    hits = [p for p in REAL_PATHS if p in src and any(w in src for w in WRITES)]
    if hits:
        unisolated.append((f.name, hits))

check("no test writes to a real user path without isolating SPENDGUARD_HOME first",
      not unisolated,
      "; ".join(f"{n} references {h}" for n, h in unisolated)
      + " — re-exec into a tempfile.mkdtemp SPENDGUARD_HOME, or redirect the path for the duration")

# And the isolation pattern must come BEFORE the package import, or the module has already resolved its
# paths from the real home and the redirect is decorative.
late = []
for f in sorted(HERE.glob("test_*.py")):
    src = f.read_text()
    if ISOLATION not in src:
        continue
    iso_at = src.index(ISOLATION)
    imp = src.find("\nfrom spendguard")
    if imp != -1 and imp < iso_at:
        late.append(f.name)
check("isolation happens BEFORE spendguard is imported",
      not late, ", ".join(late) + " — the package resolves its paths at import time")

print(f"\n{'[FAIL]' if failures else 'OK'} test_no_test_touches_real_user_data: {failures} failure(s)")
sys.exit(1 if failures else 0)

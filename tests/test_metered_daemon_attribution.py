"""A metered call bounded by a wall-clock timeout records on a DAEMON worker thread — and must still carry the
caller's intent + caller onto the ledger row.

THE BUG (found on honestreview's parallel refute fan): bulk_delegate(metered_only=True) → _run_task_on_api sets
calls.set_context(intent=…) on the worker, then adapters.call(metered_only=True, timeout_s=…). The metered branch of
_call_once runs the provider call on a DAEMON thread (the wall-clock bound), and the gated SDK / http_capture records
the call FROM that daemon — whose thread-local context is EMPTY and whose stack is threading.py:run. So every fanned
metered row landed intent=None (→ '(none)', + a "PAID call with NO intent" warning) with caller=threading.py:run,
while the lane branch (recorded on the worker) was tagged. 64 rows + 31 warnings on one refute run.

THE FIX: the daemon carries the worker's intent/chain AND caller across the boundary (the vendor_call._attempt
pattern) — calls.set_context now takes who=, and record_call prefers ctx['who'] over its own (wrong-thread) stack walk.

This reproduces it hermetically: a fake OpenAI client whose create() runs inside the daemon and records via the gate
exactly where the real bug lands. GROUNDED against the ledger (the calls table), not a fixture. No network.
"""
import os
import sys
import types
import sqlite3
import tempfile

os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="sg-daemon-")
os.environ["SPENDGUARD_CALLS"] = "1"                       # ledger on (fails closed otherwise)
os.environ.setdefault("SPENDGUARD_TEST_ISOLATED", "1")
os.environ.setdefault("SPENDGUARD_NO_AUTOINSTALL", "1")
for _k in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY"):
    os.environ[_k] = "sk-test-fake-not-real"
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from spendguard import adapters, calls, config, gate   # noqa: E402


def report_check(name, cond):
    """Print one PASS/FAIL line and return [] on pass or [name] on fail, so the caller accumulates failures."""
    print(("  [OK] " if cond else "  [FAIL] ") + name)
    return [] if cond else [name]


fails = []
INTENT = "honestreview:repro-METERED"
seen = []                                                 # what the DAEMON thread observed at record time


class _RecInDaemon:
    """A fake OpenAI client. Its create() runs inside _bounded_create's DAEMON worker (timeout_s is set), and there
    it records the metered call via the gate — exactly where the real gated SDK / http_capture records it. We capture
    what the recorder saw (intent + caller) on that daemon thread, and let gate._record_rt write the real ledger row."""

    class chat:
        class completions:
            @staticmethod
            def create(**kw):
                _cur = calls.current() or {}
                seen.append({"intent": _cur.get("intent"), "who": _cur.get("who")})
                gate._record_rt("gpt-5.6-luna", {"model": "gpt-5.6-luna"}, 5, 3, cost=0.01, provider="openai")
                return types.SimpleNamespace(
                    choices=[types.SimpleNamespace(message=types.SimpleNamespace(content="ok"), finish_reason="stop")],
                    usage=types.SimpleNamespace(prompt_tokens=5, completion_tokens=3))

    def __init__(self, **kw):
        pass

    def close(self):
        pass


def _ledger_rows(intent):
    con = sqlite3.connect(config.db_path())
    try:
        return con.execute("SELECT intent, executor, caller FROM calls WHERE kind='realtime' ORDER BY rowid").fetchall()
    finally:
        con.close()


import openai as _openai_mod   # noqa: E402
_orig = _openai_mod.OpenAI
_openai_mod.OpenAI = _RecInDaemon
try:
    # simulate the fan: the worker tags the intent, then makes a metered_only call bounded by a wall-clock timeout
    # (→ the daemon path). TWO calls = two "rows", so we prove EVERY row is tagged, not just the first.
    with calls.context(intent=INTENT):
        for _ in range(2):
            adapters.call("gpt-5.6-luna", "classify: refund please", sig=INTENT, timeout_s=5,
                          metered_only=True, max_tokens=100)
finally:
    _openai_mod.OpenAI = _orig

print("-- the provider call ran on the DAEMON and now SEES the carried intent (the fix) --")
fails += report_check("both metered calls ran (via the daemon path)", len(seen) == 2)
fails += report_check("the daemon thread saw the caller's intent (not None) on EVERY call",
                      bool(seen) and all(s["intent"] == INTENT for s in seen))
fails += report_check("the daemon carries a REAL caller in ctx['who'] (what record_call uses), not threading.py:run",
                      bool(seen) and all(s["who"] and not str(s["who"]).startswith("threading.py:run") for s in seen))

print("\n-- GROUNDED against the LEDGER: every metered row is tagged (intent + executor='api'), none in '(none)' --")
rows = _ledger_rows(INTENT)
fails += report_check("two realtime rows were recorded", len(rows) == 2)
fails += report_check("EVERY metered row records the caller's intent (not None / '(none)')",
                      bool(rows) and all(r[0] == INTENT for r in rows))
fails += report_check("every metered row records executor='api'", bool(rows) and all(r[1] == "api" for r in rows))
fails += report_check("no metered row records caller=threading.py:run (the wrong-thread stack walk)",
                      bool(rows) and all(r[2] and not str(r[2]).startswith("threading.py:run") for r in rows))

print(f"\n{'[FAIL]' if fails else 'OK'} test_metered_daemon_attribution: {len(fails)} failure(s)")
sys.exit(1 if fails else 0)

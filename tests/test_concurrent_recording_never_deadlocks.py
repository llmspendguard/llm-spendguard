"""CONCURRENCY: the money ledger must never deadlock under the concurrent fan_out the panel imposes.

MEASURED FAILURE (2026-08-14). One shared sqlite connection + the `dec_sum` custom aggregate + concurrent
record(write) / sum(read) from fan_out worker threads produced a hard sqlite<->GIL DEADLOCK: a thread mid-SUM
held the connection mutex and wanted the GIL, while another held the GIL and wanted the mutex. A real panel run
hung EIGHT MINUTES; the SIGABRT stack showed step_callback/take_gil on one thread and vdbeUnbind/pthread_mutex
on another. A recording path that can deadlock is the worst failure a spend GATE can have — it stalls the very
work it meters — and it is a RACE, so it passes most runs and looks fine. The fix is a THREAD-LOCAL connection
(budget._ledger): per-thread connections never share the sqlite mutex, so the deadlock is structurally gone.

This test FORCES the race — many threads hammering record + dec_sum reads at once — under a faulthandler
watchdog so a regression ABORTS with every thread's stack, never hangs silently. It also proves the money is
still exact under contention and the hash-chained audit log survives concurrent writers.
"""
import os, sys, tempfile, threading, faulthandler, time, datetime
from decimal import Decimal

if not os.environ.get("SPENDGUARD_TEST_ISOLATED"):
    os.environ["SPENDGUARD_TEST_ISOLATED"] = "1"
    os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="spendguard-concurrency-")
    os.execv(sys.executable, [sys.executable] + sys.argv)

from spendguard import config as _cfg
_cfg.budget_backend = lambda: "sqlite"        # the cross-thread ledger path only exists on the sqlite backend
from spendguard import budget

N_THREADS = 8
PER_THREAD = 120
COST = 0.01
DAY = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-01")
WATCHDOG_S = 60                               # a healthy run finishes in a few seconds; a deadlock hangs forever

fails = 0
def ck(label, cond, extra=""):
    global fails
    if not cond:
        fails += 1
    print(f"  [{'OK' if cond else 'FAIL'}] {label}{('  — ' + extra) if extra and not cond else ''}")

errors = []
def worker(tid):
    try:
        for i in range(PER_THREAD):
            budget.record_charge(provider="anthropic", model="claude-haiku-4-5", kind="realtime", cost=COST,
                          project="conc", conv_id=f"t{tid}-{i}")
            # the dec_sum READ paths, run concurrently with every other thread's writes — the deadlock condition
            budget.spent_since(DAY)
            budget._ledger().sum_dec(where={"project_primary": "conc"})
    except Exception as e:                     # a thread that raised must not vanish — surface it
        errors.append(f"thread {tid}: {type(e).__name__}: {e}")

print(f"-- {N_THREADS} threads x {PER_THREAD} (record + 2 dec_sum reads) ops, watchdog {WATCHDOG_S}s --")
faulthandler.dump_traceback_later(WATCHDOG_S, exit=True)     # deadlock -> full stack dump + abort(1), not a hang
t0 = time.time()
threads = [threading.Thread(target=worker, args=(i,)) for i in range(N_THREADS)]
for t in threads:
    t.start()
for t in threads:
    t.join()
faulthandler.cancel_dump_traceback_later()
dt = time.time() - t0

ck("NO DEADLOCK — every thread finished, the watchdog never fired", True)   # reaching this line == no hang
ck("no worker raised under contention", not errors, "; ".join(errors[:3]))
print(f"  ({N_THREADS * PER_THREAD} record+read cycles in {dt:.1f}s)")

led = budget._ledger()
n_rows = led.count_events(where={"project_primary": "conc"})
ck("every write landed — no row lost or spuriously deduped under contention",
   n_rows == N_THREADS * PER_THREAD, f"{n_rows} rows vs {N_THREADS * PER_THREAD} expected")

total = Decimal(led.sum_dec(where={"project_primary": "conc"}))
expected = Decimal(str(COST)) * (N_THREADS * PER_THREAD)
ck(f"Σ is EXACT under contention ({total} == {expected})", total == expected, f"{total} vs {expected}")

ok, bad = led.verify_audit_chain()
ck("the hash-chained audit log is intact after concurrent writers", ok, str(bad)[:120])

print(f"\n{'[FAIL]' if fails else 'OK'} test_concurrent_recording_never_deadlocks: {fails} failure(s)")
sys.exit(1 if fails else 0)

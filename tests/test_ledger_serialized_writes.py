"""Offline contention reproduction: concurrent audit transactions must serialize."""
import concurrent.futures
import os
import sys
import tempfile
import threading
import time
from pathlib import Path

if not os.environ.get("SPENDGUARD_TEST_ISOLATED"):
    os.environ["SPENDGUARD_TEST_ISOLATED"] = "1"
    os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="ledger-writer-contention-")
    os.execv(sys.executable, [sys.executable, "-S"] + sys.argv)

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from spendguard.ledger import SpendLedger
from spendguard import budget, config

WRITER_TEST_CONCURRENCY = 12
WRITER_COHORT_WAIT_S = config.LEDGER_WRITE_TIMEOUT_S
WRITER_TEST_TRANSACTION_HOLD_SECONDS = 0.02
WRITER_TEST_BUSY_TIMEOUT_MS = 0
WRITER_TEST_EVENT_COST = "0.01"
WRITER_TEST_PROVIDER = "offline-provider"
WRITER_TEST_MODEL = "offline-model"

ledgers = [SpendLedger() for _ in range(WRITER_TEST_CONCURRENCY)]
for ledger in ledgers:
    if ledger._conn is not None:
        ledger._conn.execute(f"PRAGMA busy_timeout={WRITER_TEST_BUSY_TIMEOUT_MS}")
try:
    from spendguard.ledger_writer import run_ledger_write, current_ledger_writer_connection
except ImportError:
    pass
else:
    run_ledger_write(lambda: current_ledger_writer_connection().execute(
        f"PRAGMA busy_timeout={WRITER_TEST_BUSY_TIMEOUT_MS}"))

original_audit = SpendLedger._audit


def hold_contention_reproduction_transaction(self, *args, **kwargs):
    time.sleep(WRITER_TEST_TRANSACTION_HOLD_SECONDS)
    return original_audit(self, *args, **kwargs)


SpendLedger._audit = hold_contention_reproduction_transaction
start = threading.Barrier(WRITER_TEST_CONCURRENCY)


def post_contention_reproduction_event(index):
    try:
        event = budget.charge_to_event(WRITER_TEST_PROVIDER, WRITER_TEST_MODEL, "realtime", WRITER_TEST_EVENT_COST)
        event["dedup_key"] = str(index)
        start.wait(timeout=WRITER_COHORT_WAIT_S)
        return ledgers[index].record_event(event)
    except BaseException:
        start.abort()
        raise


try:
    with concurrent.futures.ThreadPoolExecutor(max_workers=WRITER_TEST_CONCURRENCY) as executor:
        futures = list(executor.map(post_contention_reproduction_event, range(WRITER_TEST_CONCURRENCY)))
    assert len(set(futures)) == WRITER_TEST_CONCURRENCY
    assert ledgers[0].count_events() == WRITER_TEST_CONCURRENCY
    assert ledgers[0].verify_audit_chain()[0]
    assert not any(path.exists() for path in budget.spend_event_retry_paths())
    print("PASS: concurrent audit writes commit without database locks or deadletter growth")
finally:
    SpendLedger._audit = original_audit
    for ledger in ledgers:
        if ledger._conn is not None:
            ledger._conn.close()

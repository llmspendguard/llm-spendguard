"""Force reader/schema-dispatch versus writer/subsystem-lock inversion offline."""
import contextlib
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
from unittest.mock import patch

LOCK_ORDER_WATCHDOG_S = 5.0
LOCK_ORDER_PROBE_ARGUMENT = "--lock-order-probe"
LOCK_ORDER_PROBE_CASES = ("token-ratio", "conversation", "base-model", "samples")
LOCK_ORDER_ADDITIONAL_READ_CASES = ("brief", "backfill", "cache-combos", "cache-prompts", "cache-test")
LOCK_ORDER_CACHE_COMBO_ROWS = 3
LOCK_ORDER_SECOND_LEASE = 2


def exercise_writer_subsystem_interleaving(probe_case):
    from spendguard import backfill, brief, cacheaudit, cachetest, callio, calls, config, experiment, learn, review
    from spendguard.ledger_writer import current_ledger_writer_connection

    reader_at_schema = threading.Event()
    writer_at_lock = threading.Event()
    writer_errors = []
    subsystem = learn if probe_case in ("conversation", "backfill") else callio
    original_lock = subsystem._lock
    original_ledger_op = config.ledger_op
    reader_lease_count = 0
    synchronized_lease_number = LOCK_ORDER_SECOND_LEASE if probe_case == "cache-prompts" else 1
    if probe_case in ("cache-combos", "cache-prompts"):
        for sample_number in range(LOCK_ORDER_CACHE_COMBO_ROWS):
            callio.record_io_sample(probe_case, probe_case, probe_case, probe_case,
                                    sample_number, probe_case, probe_case)

    class ObservedWriterSubsystemLock:
        def __enter__(self):
            if current_ledger_writer_connection() is not None:
                writer_at_lock.set()
            original_lock.acquire()
            return self

        def __exit__(self, *exception_details):
            original_lock.release()

    @contextlib.contextmanager
    def synchronized_reader_schema(*args, **kwargs):
        nonlocal reader_lease_count
        reader_lease_count += 1
        if reader_lease_count == synchronized_lease_number:
            reader_at_schema.set()
            assert writer_at_lock.wait(LOCK_ORDER_WATCHDOG_S), "writer never reached subsystem lock"
        with original_ledger_op(*args, **kwargs) as connection:
            yield connection

    def persist_competing_subsystem_row():
        try:
            assert reader_at_schema.wait(LOCK_ORDER_WATCHDOG_S)
            if subsystem is learn:
                learn.add_node(probe_case, probe_case)
            else:
                callio.record_io_sample(None, probe_case, probe_case, probe_case,
                                        probe_case, "", "")
        except BaseException as error:
            writer_errors.append(error)

    with patch.object(subsystem, "_lock", ObservedWriterSubsystemLock()), \
            patch.object(config, "ledger_op", synchronized_reader_schema), \
            patch.object(calls, "cost_summary", return_value=[]), \
            patch.object(callio, "good_rates", return_value={}), \
            patch.object(learn, "insights", return_value=[]), \
            patch.object(cacheaudit, "_count_tokens", side_effect=lambda text, model: len(text)):
        competing_writer = threading.Thread(target=persist_competing_subsystem_row, daemon=True)
        competing_writer.start()
        if probe_case == "token-ratio":
            review._token_ratio(None, probe_case)
        elif probe_case == "conversation":
            review._conversation_for(probe_case)
        elif probe_case == "base-model":
            experiment._base_model(probe_case)
        elif probe_case == "samples":
            experiment._samples(probe_case, probe_case, len(LOCK_ORDER_PROBE_CASES))
        elif probe_case == "brief":
            brief._defaults(probe_case, probe_case)
        elif probe_case == "backfill":
            backfill.backfill(providers=())
        elif probe_case in ("cache-combos", "cache-prompts"):
            cacheaudit._intent_prefixes()
        else:
            cachetest._system_and_users_from_intent(probe_case, len(LOCK_ORDER_PROBE_CASES))
        competing_writer.join(LOCK_ORDER_WATCHDOG_S)
        assert not competing_writer.is_alive(), "competing write deadlocked"
        assert not writer_errors, writer_errors


def test_writer_subsystem_lock_order_deadlock_reproduction():
    for probe_case in LOCK_ORDER_PROBE_CASES + LOCK_ORDER_ADDITIONAL_READ_CASES:
        with tempfile.TemporaryDirectory() as isolated_home:
            environment = dict(os.environ, SPENDGUARD_HOME=isolated_home, SPENDGUARD_TEST_ISOLATED="1")
            try:
                completed = subprocess.run(
                    [sys.executable, str(Path(__file__).resolve()), LOCK_ORDER_PROBE_ARGUMENT, probe_case],
                    env=environment, capture_output=True, text=True,
                    timeout=LOCK_ORDER_WATCHDOG_S * len(LOCK_ORDER_PROBE_CASES))
            except subprocess.TimeoutExpired as error:
                raise AssertionError(f"{probe_case}: subsystem lock/writer dispatch deadlocked") from error
            assert completed.returncode == 0, (probe_case, completed.stdout, completed.stderr)
            print(f"{probe_case}: reader and competing writer completed")


if __name__ == "__main__":
    if LOCK_ORDER_PROBE_ARGUMENT in sys.argv:
        exercise_writer_subsystem_interleaving(sys.argv[-1])
    else:
        test_writer_subsystem_lock_order_deadlock_reproduction()

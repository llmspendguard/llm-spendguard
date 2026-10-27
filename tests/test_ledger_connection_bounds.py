"""Offline script guards for process admission, per-open schema repair and isolation."""
import concurrent.futures
import gc
import os
import runpy
import sqlite3
import sys
import tempfile
import threading
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("SPENDGUARD_TEST_ISOLATED", "1")
os.environ.setdefault("SPENDGUARD_HOME", tempfile.mkdtemp())

from spendguard import config
from spendguard.ledger import SpendLedger
from spendguard import lane_economics, lane_queue
from spendguard.ledger_writer import close_ledger_writers

BOUNDS_SINGLE_SLOT_CEILING = 1
BOUNDS_MEMORY_DATABASE = ":memory:"
BOUNDS_FORK_POOL_KEY = "bounds-fork-parent"
BOUNDS_TEST_CEILING = 3
BOUNDS_WRITER_OWNERS = 1
BOUNDS_FORK_CEILING = BOUNDS_SINGLE_SLOT_CEILING + BOUNDS_WRITER_OWNERS
BOUNDS_TEST_WORKERS = 4
BOUNDS_TEST_SCHEMA = "CREATE TABLE IF NOT EXISTS bounds_probe(value INTEGER)"


def check_ledger_bounds(label, condition, failures):
    print(f"[{'OK' if condition else 'FAIL'}] {label}")
    if not condition:
        failures.append(label)


def ensure_bounds_probe(connection):
    connection.execute(BOUNDS_TEST_SCHEMA)


def reject_bounds_schema(connection):
    raise RuntimeError("schema initialization failed")


def open_bounds_worker(path):
    try:
        return config.pooled_ledger_conn("bounds-worker", ensure_bounds_probe, path)
    except config.LedgerConnectionLimitError:
        return None


def run_ledger_bounds_checks(failures):
    # Deterministically emulate allocator address reuse in an isolated config
    # namespace. Both old and replacement handles report the same identity;
    # finalizing the closed old handle must not release the replacement's slot.
    recycled_identity = object()
    recycled_config = runpy.run_path(config.__file__, init_globals={
        "id": lambda connection: recycled_identity,
    })
    recycled_open = recycled_config["open_ledger_connection"]
    with patch.dict(recycled_open.__globals__, ledger_connection_limit=lambda: BOUNDS_SINGLE_SLOT_CEILING):
        stale_handle = recycled_open(BOUNDS_MEMORY_DATABASE, tune=False)
        stale_handle.close()
        current_handle = recycled_open(BOUNDS_MEMORY_DATABASE, tune=False)
        stale_handle.__del__()
        check_ledger_bounds("recycled identity retains live replacement count",
                            recycled_config["ledger_connection_count"]() == BOUNDS_SINGLE_SLOT_CEILING, failures)
        try:
            overflow_handle = recycled_open(BOUNDS_MEMORY_DATABASE, tune=False)
        except recycled_config["LedgerConnectionLimitError"]:
            check_ledger_bounds("recycled identity cannot bypass ceiling", True, failures)
        else:
            overflow_handle.close()
            check_ledger_bounds("recycled identity cannot bypass ceiling", False, failures)
        current_handle.close()

    with tempfile.TemporaryDirectory() as bounds_dir:
        bounds_path = Path(bounds_dir) / "bounds.sqlite"
        baseline = config.ledger_connection_count()
        with patch.object(config, "_cfg_get", return_value=BOUNDS_TEST_CEILING):
            check_ledger_bounds("ceiling is read from ledger configuration",
                                config.ledger_connection_limit() == BOUNDS_TEST_CEILING, failures)
        with patch.object(config, "_cfg_get", return_value=0):
            try:
                config.ledger_connection_limit()
            except ValueError:
                check_ledger_bounds("invalid ceiling fails closed", True, failures)
            else:
                check_ledger_bounds("invalid ceiling fails closed", False, failures)
        with patch.object(config, "ledger_connection_limit", return_value=baseline + BOUNDS_WRITER_OWNERS + BOUNDS_TEST_CEILING):
            config.fresh_ledger_conn(ensure_bounds_probe, bounds_path).close()
            baseline = config.ledger_connection_count()
            connections = [config.fresh_ledger_conn(ensure_bounds_probe, bounds_path)
                           for _ in range(BOUNDS_TEST_CEILING)]
            check_ledger_bounds("live count includes fresh connections",
                                config.ledger_connection_count() == baseline + BOUNDS_TEST_CEILING, failures)
            for opener in (lambda: config.fresh_ledger_conn(ensure_bounds_probe, bounds_path),
                           lambda: config.pooled_ledger_conn("overflow", ensure_bounds_probe, bounds_path),
                           lane_economics._cum_tokens_by_executor):
                try:
                    unexpected = opener()
                except config.LedgerConnectionLimitError as exc:
                    check_ledger_bounds("ceiling raises with ceiling and live count",
                                        str(baseline + BOUNDS_TEST_CEILING) in str(exc), failures)
                else:
                    check_ledger_bounds("ceiling rejects all managed openers", False, failures)
                    getattr(unexpected, "_conn", unexpected).close()
            connections.pop().close()
            replacement = config.fresh_ledger_conn(ensure_bounds_probe, bounds_path)
            check_ledger_bounds("close releases a slot", config.ledger_connection_count() == baseline + BOUNDS_TEST_CEILING, failures)
            stale_connection = connections.pop()
            stale_connection.close()
            live_replacement = config.fresh_ledger_conn(ensure_bounds_probe, bounds_path)
            stale_connection.__del__()
            check_ledger_bounds("stale finalization preserves replacement slot",
                                config.ledger_connection_count() == baseline + BOUNDS_TEST_CEILING, failures)
            try:
                config.open_ledger_connection(bounds_path)
            except config.LedgerConnectionLimitError:
                check_ledger_bounds("stale finalization cannot bypass ceiling", True, failures)
            else:
                check_ledger_bounds("stale finalization cannot bypass ceiling", False, failures)
            live_replacement.close()
            replacement.close()
            replacement.close()
            for connection in connections:
                connection.close()
            close_ledger_writers()
            baseline -= BOUNDS_WRITER_OWNERS
        check_ledger_bounds("double close does not corrupt count", config.ledger_connection_count() == baseline, failures)
        try:
            config.fresh_ledger_conn(reject_bounds_schema, bounds_path)
        except RuntimeError:
            pass
        close_ledger_writers()
        check_ledger_bounds("failed setup releases admission", config.ledger_connection_count() == baseline, failures)

        # Hold worker results so thread teardown cannot hide admission/accounting bugs.
        start = threading.Barrier(BOUNDS_TEST_WORKERS)

        def contend_bounds_worker():
            start.wait()
            return open_bounds_worker(bounds_path)

        with patch.object(config, "ledger_connection_limit", return_value=baseline + BOUNDS_TEST_CEILING + BOUNDS_WRITER_OWNERS):
            config.fresh_ledger_conn(ensure_bounds_probe, bounds_path).close()
            with concurrent.futures.ThreadPoolExecutor(max_workers=BOUNDS_TEST_WORKERS) as workers:
                worker_results = list(workers.map(lambda _: contend_bounds_worker(), range(BOUNDS_TEST_WORKERS)))
            admitted = [c for c in worker_results if c is not None]
            check_ledger_bounds("concurrent admission never overshoots ceiling", len(admitted) == BOUNDS_TEST_CEILING, failures)
            check_ledger_bounds("thread teardown counts externally retained connections",
                                config.ledger_connection_count() == baseline + len(admitted) + BOUNDS_WRITER_OWNERS, failures)
            for connection in admitted:
                connection.close()
            close_ledger_writers()

        traced = []
        original_connect = sqlite3.connect

        def trace_bounds_connection(*args, **kwargs):
            connection = original_connect(*args, **kwargs)
            connection.set_trace_callback(traced.append)
            return connection

        schema_path = Path(bounds_dir) / "schema.sqlite"
        with patch.object(sqlite3, "connect", side_effect=trace_bounds_connection):
            first = SpendLedger(schema_path)
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as worker:
                second = worker.submit(SpendLedger, schema_path).result()
            table_queries = [s for s in traced if s == "PRAGMA table_info(spend_events)"]
            check_ledger_bounds("SpendLedger table_info runs once per instance across threads", len(table_queries) == 2, failures)
            check_ledger_bounds("SpendLedger index_list runs once per instance across threads",
                                traced.count("PRAGMA index_list(spend_events)") == 2, failures)
            check_ledger_bounds("SpendLedger columns agree across ensured connections", set(first._cols) == set(second._cols), failures)
            close_ledger_writers()
            schema_path.unlink()
            recreated = SpendLedger(schema_path)
            check_ledger_bounds("recreated database is ensured again",
                                len([s for s in traced if s == "PRAGMA table_info(spend_events)"]) == 3, failures)
            close_ledger_writers()

        # A DROP keeps the file identity intact but invalidates every schema fact.
        queue_path = Path(bounds_dir) / "queue-repair.sqlite"
        queue_before = config.fresh_ledger_conn(lane_queue._ensure_queue_schema, queue_path)
        queue_columns = tuple(row[1] for row in queue_before.execute("PRAGMA table_info(lane_queue)"))
        queue_indexes = tuple(row[1] for row in queue_before.execute("PRAGMA index_list(lane_queue)"))
        queue_identity = (queue_path.stat().st_dev, queue_path.stat().st_ino)
        queue_before.execute("DROP TABLE lane_queue")
        queue_before.commit()
        queue_before.close()
        queue_after = config.fresh_ledger_conn(lane_queue._ensure_queue_schema, queue_path)
        check_ledger_bounds("same-file drop preserves file identity",
                            queue_identity == (queue_path.stat().st_dev, queue_path.stat().st_ino), failures)
        check_ledger_bounds("same-file drop restores base and all additive queue columns",
                            queue_columns == tuple(row[1] for row in queue_after.execute("PRAGMA table_info(lane_queue)")), failures)
        check_ledger_bounds("same-file drop restores all required queue indexes",
                            set(queue_indexes) == {row[1] for row in queue_after.execute("PRAGMA index_list(lane_queue)")}, failures)
        queue_after.execute("INSERT INTO lane_queue(intent, task, sla_class) VALUES (?, ?, ?)",
                            (ensure_bounds_probe.__name__, ensure_bounds_probe.__name__, ensure_bounds_probe.__name__))
        queue_after.commit()
        queue_after.close()
        queue_durable = config.fresh_ledger_conn(lane_queue._ensure_queue_schema, queue_path)
        check_ledger_bounds("same-file repaired queue write is durable on next open",
                            queue_durable.execute("SELECT COUNT(*) FROM lane_queue").fetchone()[0] == 1, failures)
        queue_durable.close()

        setup_calls = []

        def ensure_bounds_counted(connection):
            setup_calls.append(connection)
            ensure_bounds_probe(connection)

        outer = config.pooled_ledger_conn("bounds-outer", ensure_bounds_counted, bounds_path)
        inner = config.pooled_ledger_conn("bounds-inner", ensure_bounds_counted, bounds_path)
        check_ledger_bounds("cross-subsystem connections remain distinct", outer is not inner, failures)
        check_ledger_bounds("schema setup runs for each subsystem connection", len(setup_calls) == 2, failures)
        outer.execute("INSERT INTO bounds_probe VALUES (?)", (BOUNDS_TEST_CEILING,))
        with config.ledger_op("bounds-inner", ensure_bounds_counted, bounds_path):
            pass
        check_ledger_bounds("nested subsystem commit cannot commit outer transaction", outer.in_transaction, failures)
        outer.rollback()
        inner = config.pooled_ledger_conn("bounds-inner", ensure_bounds_counted, bounds_path)
        check_ledger_bounds("outer rollback remains effective", inner.execute("SELECT COUNT(*) FROM bounds_probe").fetchone()[0] == 0, failures)
        ceiling = config.ledger_connection_limit()
        check_ledger_bounds("cache budget fits process ceiling",
                            -outer.execute("PRAGMA cache_size").fetchone()[0] * ceiling <= config.LEDGER_CACHE_BUDGET_KIB, failures)
        check_ledger_bounds("mmap budget fits process ceiling",
                            outer.execute("PRAGMA mmap_size").fetchone()[0] * ceiling <= config.LEDGER_MMAP_BUDGET_BYTES, failures)
        config.reset_ledger_conn("bounds-outer")
        config.reset_ledger_conn("bounds-inner")
        close_ledger_writers()
        baseline = config.ledger_connection_count()
        transient = config.open_ledger_connection(bounds_path)
        del transient
        gc.collect()
        check_ledger_bounds("collection releases unreferenced connections", config.ledger_connection_count() == baseline, failures)
        inherited = config.open_ledger_connection(bounds_path, tune=False)
        with patch.object(config, "ledger_connection_limit", return_value=ceiling + 1):
            try:
                resized = config.open_ledger_connection(bounds_path)
            except config.LedgerConnectionLimitError:
                check_ledger_bounds("live ceiling changes cannot overallocate memory", True, failures)
            else:
                resized.close()
                check_ledger_bounds("live ceiling changes cannot overallocate memory", False, failures)
        check_ledger_bounds("untuned readers share the cache budget",
                            -inherited.execute("PRAGMA cache_size").fetchone()[0] * ceiling <= config.LEDGER_CACHE_BUDGET_KIB, failures)
        inherited.close()
        with patch.object(config, "ledger_connection_limit", return_value=BOUNDS_FORK_CEILING):
            inherited = config.pooled_ledger_conn(BOUNDS_FORK_POOL_KEY, ensure_bounds_probe, bounds_path)
            parent_generation = config._LEDGER_ADMISSION
            config._abandon_inherited_ledger_generation_in_fork_child()
            check_ledger_bounds("same-process hook preserves live accounting",
                                config._LEDGER_ADMISSION is parent_generation
                                and config.ledger_connection_count() == BOUNDS_FORK_CEILING, failures)
            sys.stdout.flush()
            child_pid = os.fork()
            if child_pid:
                _, child_status = os.waitpid(child_pid, 0)
                check_ledger_bounds("real fork child admission checks pass", os.waitstatus_to_exitcode(child_status) == 0, failures)
                check_ledger_bounds("fork leaves parent generation and handle intact",
                                    config._LEDGER_ADMISSION is parent_generation
                                    and config.ledger_connection_count() == BOUNDS_FORK_CEILING
                                    and inherited.execute("SELECT COUNT(*) FROM bounds_probe").fetchone()[0] == 0, failures)
                config.reset_ledger_conn(BOUNDS_FORK_POOL_KEY)
                return
            check_ledger_bounds("child records abandoned inherited count",
                                config._LEDGER_ADMISSION.abandoned_inherited_count == BOUNDS_FORK_CEILING, failures)
            check_ledger_bounds("fork reset abandons inherited accounting", config.ledger_connection_count() == 0, failures)
            child_connections = [config.pooled_ledger_conn(BOUNDS_FORK_POOL_KEY, ensure_bounds_probe, bounds_path)]
            check_ledger_bounds("fork reset abandons inherited pooled handle",
                                child_connections[0] is not inherited, failures)
            inherited.__del__()
            inherited.close()
            check_ledger_bounds("inherited finalization cannot release child slots",
                                config.ledger_connection_count() == BOUNDS_FORK_CEILING, failures)
            try:
                config.open_ledger_connection(bounds_path)
            except config.LedgerConnectionLimitError:
                check_ledger_bounds("child ceiling blocks after stale finalization", True, failures)
            else:
                check_ledger_bounds("child ceiling blocks after stale finalization", False, failures)
            config.reset_ledger_conn(BOUNDS_FORK_POOL_KEY)
            sys.stdout.flush()
            os._exit(bool(failures))


if __name__ == "__main__":
    bounds_failures = []
    run_ledger_bounds_checks(bounds_failures)
    print(f"ledger connection bounds: {len(bounds_failures)} failure(s)")
    sys.exit(1 if bounds_failures else 0)

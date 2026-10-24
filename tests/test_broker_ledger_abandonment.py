"""A disconnected broker peer releases its own WAL snapshot, without recycling."""
import pathlib
import socket
import sqlite3
import tempfile
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from _pytest_fixture_bridge import align_test_startup_home

align_test_startup_home(prefix=__name__)

from spendguard import codex_daemon, codex_daemon_broker, config, ledger_read, mcp_server
from spendguard import agent_spawn_gate, residency_gate, weekly_cap_guard

BROKER_WAL_WAIT_S = 5.0
BROKER_WAL_READ_DEADLINE_S = BROKER_WAL_WAIT_S * 4
BROKER_WAL_RECURSIVE_STEPS = 1000000000
BROKER_WAL_SCHEMA = "CREATE TABLE broker_snapshot_probe (value)"
BROKER_WAL_INSERT = "INSERT INTO broker_snapshot_probe VALUES (?)"
BROKER_WAL_SELECT = "SELECT * FROM broker_snapshot_probe"
BROKER_WAL_SLOW_SQL = """WITH RECURSIVE slow(value) AS
    (VALUES(0) UNION ALL SELECT value+1 FROM slow WHERE value<?)
    SELECT SUM(value) FROM slow"""
BROKER_WAL_CHECKPOINT_SQL = "PRAGMA wal_checkpoint(TRUNCATE)"
POOL_WAL_WATCHDOG_S = 1.0
POOL_WAL_FIXTURE_SIZE = 1


class BrokerLedgerAbandonmentGuards(unittest.TestCase):
    def test_pool_watchdog_guard_rejects_disabled_cancellation_mutant(self):
        scopes = []
        original_cancel = ledger_read.LedgerRequestReadScope.cancel_ledger_request

        def omit_pool_mutant_cancellation(scope):
            scopes.append(scope)

        try:
            with patch.object(ledger_read.LedgerRequestReadScope, "cancel_ledger_request",
                              omit_pool_mutant_cancellation):
                with self.assertRaisesRegex(AssertionError, "detached its ledger read"):
                    self.test_pool_watchdog_interrupts_inflight_ledger_snapshot_before_recycle()
        finally:
            for scope in scopes:
                original_cancel(scope)

    def test_hook_budget_deadlines_deny_instead_of_allowing(self):
        deadline = ledger_read.LedgerQueryDeadline(BROKER_WAL_READ_DEADLINE_S)
        with patch.object(agent_spawn_gate, "_plan_in_band", side_effect=deadline):
            result = agent_spawn_gate.evaluate_pretooluse({})
        self.assertEqual(result["hookSpecificOutput"]["permissionDecision"], "deny")
        with patch.object(weekly_cap_guard, "_weekly_cap_guard_spawn", return_value=True), \
                patch.object(config, "disabled", return_value=False), \
                patch.object(weekly_cap_guard, "anthropic_weekly_headroom", side_effect=deadline):
            result = weekly_cap_guard.weekly_cap_guard_pretooluse({})
        self.assertEqual(result["hookSpecificOutput"]["permissionDecision"], "deny")
        from spendguard import claudecode
        with patch.object(claudecode, "context_trajectory", side_effect=deadline):
            result = residency_gate.evaluate_posttooluse({})
        self.assertEqual(result, {}, "the PostToolUse advisory must fail open and stay silent")

    def test_pool_watchdog_interrupts_inflight_ledger_snapshot_before_recycle(self):
        ledger_path = pathlib.Path(tempfile.mkdtemp(dir=config.HOME)) / pathlib.Path(__file__).stem
        writer = sqlite3.connect(ledger_path)
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute("PRAGMA wal_autocheckpoint=0")
        writer.execute(BROKER_WAL_SCHEMA)
        writer.execute(BROKER_WAL_INSERT, (__name__,))
        writer.commit()
        started = threading.Event()
        released = threading.Event()
        failures = []
        results = []
        invocation_count = 0

        def invoke_pool_snapshot_fixture(**kwargs):
            nonlocal invocation_count
            invocation_count += 1
            if invocation_count > 1:
                with ledger_read._ledger_read_connection(
                        ledger_path, BROKER_WAL_READ_DEADLINE_S) as next_reader:
                    return {"text": next_reader.execute(BROKER_WAL_SELECT).fetchone()[0]}
            try:
                with ledger_read._ledger_read_connection(
                        ledger_path, BROKER_WAL_READ_DEADLINE_S) as reader:
                    reader.execute("BEGIN")
                    reader.execute(BROKER_WAL_SELECT).fetchall()
                    started.set()
                    reader.execute(BROKER_WAL_SLOW_SQL,
                                   (BROKER_WAL_RECURSIVE_STEPS,)).fetchall()
            except ledger_read.LedgerReadError as failure:
                failures.append(failure)
                return {"error": str(failure)}
            finally:
                released.set()
            raise AssertionError("slow fixture unexpectedly completed before watchdog")

        def create_pool_snapshot_worker():
            return SimpleNamespace(run_warm=invoke_pool_snapshot_fixture,
                                   ensure_running=Mock(return_value=object()),
                                   shutdown=Mock(), running=Mock(return_value=True))

        pool = codex_daemon._CodexDaemonPool(create_pool_snapshot_worker, size=POOL_WAL_FIXTURE_SIZE)
        caller = threading.Thread(target=lambda: results.append(
            pool.run_warm(__name__, timeout=POOL_WAL_WATCHDOG_S)), daemon=True)
        try:
            with patch.object(config, "LEDGER_QUERY_DEADLINE_S", BROKER_WAL_READ_DEADLINE_S):
                caller.start()
                self.assertTrue(started.wait(BROKER_WAL_WAIT_S))
                writer.execute(BROKER_WAL_INSERT, (BROKER_WAL_SCHEMA,))
                writer.commit()
                writer.execute("PRAGMA busy_timeout=0")
                self.assertNotEqual(writer.execute(BROKER_WAL_CHECKPOINT_SQL).fetchone()[0], 0)
                caller.join(BROKER_WAL_WAIT_S)
                self.assertFalse(caller.is_alive())
                self.assertIn("deadline expired", results[0]["error"])
                self.assertTrue(released.wait(BROKER_WAL_WAIT_S),
                                "watchdog recycled the worker but detached its ledger read")
                self.assertTrue(failures, "watchdog must surface typed read abandonment")
                self.assertEqual(writer.execute(BROKER_WAL_CHECKPOINT_SQL).fetchone(), (0, 0, 0))
                next_result = pool.run_warm(__name__, timeout=POOL_WAL_WATCHDOG_S)
                self.assertEqual(next_result, {"text": __name__},
                                 "abandoned scan starved the next pool lease or ledger read")
        finally:
            caller.join(BROKER_WAL_READ_DEADLINE_S)
            pool.shutdown()
            writer.close()

    def test_broker_peer_eof_interrupts_owned_snapshot_without_worker_recycle(self):
        self._assert_transport_abandonment_releases_snapshot(use_mcp=False)

    def test_mcp_peer_eof_interrupts_owned_snapshot(self):
        self._assert_transport_abandonment_releases_snapshot(use_mcp=True)

    def _assert_transport_abandonment_releases_snapshot(self, *, use_mcp):
        ledger_path = pathlib.Path(tempfile.mkdtemp(dir=config.HOME)) / pathlib.Path(__file__).stem
        writer = sqlite3.connect(ledger_path)
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute("PRAGMA wal_autocheckpoint=0")
        writer.execute(BROKER_WAL_SCHEMA)
        writer.execute(BROKER_WAL_INSERT, (__name__,))
        writer.commit()
        reader_started = threading.Event()
        read_released = threading.Event()
        reader_failures = []

        def invoke_broker_snapshot_fixture(**kwargs):
            try:
                with ledger_read._ledger_read_connection(
                        ledger_path, BROKER_WAL_READ_DEADLINE_S) as reader:
                    reader.execute("BEGIN")
                    reader.execute(BROKER_WAL_SELECT).fetchall()
                    reader_started.set()
                    reader.execute(BROKER_WAL_SLOW_SQL,
                                   (BROKER_WAL_RECURSIVE_STEPS,)).fetchall()
            except ledger_read.LedgerReadError as failure:
                reader_failures.append(failure)
                return {"error": str(failure)}
            finally:
                read_released.set()
            raise AssertionError("slow fixture unexpectedly finished before abandonment")

        local = SimpleNamespace(run_warm=invoke_broker_snapshot_fixture)
        broker = codex_daemon_broker._CodexDaemonBroker(local)
        server, client = socket.socketpair()
        self.assertTrue(broker._handler_slots.acquire(blocking=False))
        def invoke_mcp_snapshot_boundary():
            with mcp_server._mcp_http_request_ledger_lifetime(server):
                local.run_warm()

        worker = threading.Thread(
            target=invoke_mcp_snapshot_boundary if use_mcp else broker._serve_one,
            args=() if use_mcp else (server,), daemon=True)
        try:
            with patch.object(config, "LEDGER_QUERY_DEADLINE_S", BROKER_WAL_READ_DEADLINE_S):
                if not use_mcp:
                    codex_daemon_broker._send_frame(client, {
                        "version": codex_daemon_broker._PROTOCOL_VERSION,
                        "method": "run_warm", "kwargs": {}})
                worker.start()
                self.assertTrue(reader_started.wait(BROKER_WAL_WAIT_S))
                writer.execute(BROKER_WAL_INSERT, (BROKER_WAL_SCHEMA,))
                writer.commit()
                writer.execute("PRAGMA busy_timeout=0")
                self.assertNotEqual(writer.execute(BROKER_WAL_CHECKPOINT_SQL).fetchone()[0], 0,
                                    "fault fixture must really pin the WAL")
                client.close()
                self.assertTrue(read_released.wait(BROKER_WAL_WAIT_S),
                                "peer abandonment left the SQLite operation running")
                worker.join(BROKER_WAL_WAIT_S)
                self.assertFalse(worker.is_alive())
                self.assertTrue(reader_failures, "abandonment must surface a typed refusal")
                self.assertEqual(writer.execute(BROKER_WAL_CHECKPOINT_SQL).fetchone(), (0, 0, 0))
                if not use_mcp:
                    self.assertTrue(broker._handler_slots.acquire(blocking=False))
                    broker._handler_slots.release()
        finally:
            client.close()
            worker.join(BROKER_WAL_READ_DEADLINE_S)
            server.close()
            writer.close()


if __name__ == "__main__":
    unittest.main()

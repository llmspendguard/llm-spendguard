"""Offline phase-one cap, bounded FIFO, frame deadline and watchdog regression guards."""
import os
import socket
import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from spendguard import codex_daemon, codex_daemon_broker, config
from spendguard.dispatch import DispatchTimeout, _Bucket

ADMISSION_TEST_WAIT_S = 5.0
ADMISSION_TEST_READ_S = 0.05
ADMISSION_TEST_TURN_S = 0.05
ADMISSION_TEST_WAITER_COUNT = 2
ADMISSION_TEST_POOL_SIZE = 1
ADMISSION_TEST_QUEUE_KEY = "offline-codex-admission"
ADMISSION_TEST_PROMPT = "offline admission fixture"
ADMISSION_TEST_REPLY = {"text": "admitted", "error": None}


class CodexAdmissionBoundsGuards(unittest.TestCase):
    def test_admission_config_defaults_overrides_and_validation(self):
        with patch.dict(os.environ, {}, clear=True), patch.object(config, "_cfg", return_value={}):
            settings = config.codex_daemon_admission_settings()
            self.assertEqual(settings["max_waiters"], config.CODEX_DAEMON_MAX_WAITERS_DEFAULT)
            self.assertEqual(settings["request_read_timeout_s"], config.CODEX_DAEMON_REQUEST_READ_TIMEOUT_DEFAULT_S)
            self.assertIs(settings["recycle_on_timeout"], True)
            with patch.object(config, "_cfg", return_value={"advisor": {
                    "codex_daemon_max_waiters": ADMISSION_TEST_WAITER_COUNT,
                    "codex_daemon_request_read_timeout_s": ADMISSION_TEST_READ_S,
                    "codex_daemon_recycle_on_timeout": False}}):
                settings = config.codex_daemon_admission_settings()
                self.assertEqual(settings["max_waiters"], ADMISSION_TEST_WAITER_COUNT)
                self.assertEqual(settings["request_read_timeout_s"], ADMISSION_TEST_READ_S)
                self.assertIs(settings["recycle_on_timeout"], False)
                with patch.dict(os.environ, {"SPENDGUARD_CODEX_DAEMON_MAX_WAITERS": "0",
                                            "SPENDGUARD_CODEX_DAEMON_RECYCLE_ON_TIMEOUT": "true"}):
                    self.assertEqual(config.codex_daemon_admission_settings()["max_waiters"], 0)
                    self.assertIs(config.codex_daemon_admission_settings()["recycle_on_timeout"], True)
            for invalid in ("0", "-1", "inf", "nan"):
                with patch.dict(os.environ, {"SPENDGUARD_CODEX_DAEMON_REQUEST_READ_TIMEOUT_S": invalid}):
                    with self.assertRaises(ValueError):
                        config.codex_daemon_admission_settings()
            with patch.dict(os.environ, {"SPENDGUARD_CODEX_DAEMON_MAX_WAITERS": "-1"}):
                with self.assertRaises(ValueError):
                    config.codex_daemon_admission_settings()
            with patch.dict(os.environ, {"SPENDGUARD_CODEX_DAEMON_RECYCLE_ON_TIMEOUT": "false"}):
                self.assertIs(config.codex_daemon_admission_settings()["recycle_on_timeout"], False)

    def test_broker_unreachable_never_invokes_local_start_or_turn(self):
        local = Mock()
        broker = codex_daemon_broker._CodexDaemonBroker(local)
        with patch.object(broker, "_become_owner_if_available", return_value=False), \
                patch.object(broker, "_call_owner", return_value=None), \
                patch.object(broker, "broker_running", return_value=False):
            self.assertIn("refused", broker.broker_run_warm(ADMISSION_TEST_PROMPT)["error"])
            self.assertIs(broker.broker_ensure_running(), False)
        local.run_warm.assert_not_called()
        local.ensure_running.assert_not_called()

    def test_codex_fifo_bound_order_timeout_and_permit_reuse(self):
        bucket = _Bucket(ADMISSION_TEST_QUEUE_KEY, ADMISSION_TEST_POOL_SIZE, rpm=0,
                         max_waiters=ADMISSION_TEST_WAITER_COUNT)
        bucket.acquire(ADMISSION_TEST_WAIT_S)
        order = []
        errors = []

        def admission_ordered_waiter(index):
            try:
                bucket.acquire(ADMISSION_TEST_WAIT_S)
                order.append(index)
                bucket.release()
            except BaseException as exc:
                errors.append(exc)

        threads = []
        condition, _ = bucket._bounded_fifo
        try:
            for index in range(ADMISSION_TEST_WAITER_COUNT):
                thread = threading.Thread(target=admission_ordered_waiter, args=(index,), daemon=True)
                threads.append(thread)
                thread.start()
                with condition:
                    self.assertTrue(condition.wait_for(
                        lambda expected=index + 1: bucket.waiting == expected, ADMISSION_TEST_WAIT_S))
            with self.assertRaisesRegex(DispatchTimeout, "waiter limit"):
                bucket.acquire(ADMISSION_TEST_WAIT_S)
            self.assertEqual(bucket.waiting, ADMISSION_TEST_WAITER_COUNT)
        finally:
            bucket.release()
            for thread in threads:
                thread.join(ADMISSION_TEST_WAIT_S)
                self.assertFalse(thread.is_alive())
        self.assertFalse(errors, errors)
        self.assertEqual(order, list(range(ADMISSION_TEST_WAITER_COUNT)))
        bucket.acquire(ADMISSION_TEST_WAIT_S)
        with self.assertRaisesRegex(DispatchTimeout, "deadline"):
            bucket.acquire(ADMISSION_TEST_TURN_S)
        self.assertEqual(bucket.waiting, 0)
        bucket.release()
        bucket.acquire(ADMISSION_TEST_WAIT_S)
        bucket.release()
        self.assertEqual(bucket.in_flight, 0)

    def test_broker_sheds_excess_connections_and_releases_read_timeout_slot(self):
        settings = {"max_waiters": 0, "request_read_timeout_s": ADMISSION_TEST_READ_S,
                    "recycle_on_timeout": True}
        with patch.object(config, "codex_daemon_admission_settings", return_value=settings), \
                patch.object(config, "codex_daemon_pool_settings", return_value={"size": ADMISSION_TEST_POOL_SIZE}):
            local = Mock()
            broker = codex_daemon_broker._CodexDaemonBroker(local)
            first_server, first_client = socket.socketpair()
            excess_server, excess_client = socket.socketpair()
            broker._listener = Mock()
            broker._listener.accept.side_effect = [(first_server, None), (excess_server, None), OSError()]
            first_client.settimeout(ADMISSION_TEST_WAIT_S)
            excess_client.settimeout(ADMISSION_TEST_WAIT_S)
            try:
                broker._serve_connections()
                refusal = codex_daemon_broker._receive_frame(excess_client)
                self.assertIn("waiter limit", refusal["result"]["error"])
                timed_out = codex_daemon_broker._receive_frame(first_client)
                self.assertIn("refused", timed_out["result"]["error"])
                # EOF follows handler cleanup, including release of its permit.
                self.assertEqual(first_client.recv(1), b"")
                self.assertTrue(broker._handler_slots.acquire(blocking=False))
                broker._handler_slots.release()
                local.run_warm.assert_not_called()
            finally:
                first_client.close()
                excess_client.close()
                first_server.close()
                excess_server.close()

    def test_broker_protocol_refusals_and_response_write_deadline(self):
        local = Mock()
        local.CALL_TIMEOUT_S = ADMISSION_TEST_WAIT_S
        local.STARTUP_TIMEOUT_S = ADMISSION_TEST_WAIT_S
        local.run_warm.return_value = dict(ADMISSION_TEST_REPLY)
        broker = codex_daemon_broker._CodexDaemonBroker(local)
        read_timeout = config.codex_daemon_admission_settings()["request_read_timeout_s"]
        for kwargs in ([], {"prompt": ADMISSION_TEST_PROMPT}):
            connection = Mock()
            request = {"version": codex_daemon_broker._PROTOCOL_VERSION, "method": "run_warm", "kwargs": kwargs}
            self.assertTrue(broker._handler_slots.acquire(blocking=False))
            with patch.object(codex_daemon_broker, "_receive_frame", return_value=request), \
                    patch.object(codex_daemon_broker, "_send_frame") as send:
                broker._serve_one(connection)
            self.assertEqual(connection.settimeout.call_args.args, (read_timeout,))
            result = send.call_args.args[1]["result"]
            if isinstance(kwargs, list):
                self.assertIn("invalid request refused", result["error"])
            else:
                self.assertEqual(result, ADMISSION_TEST_REPLY)
            connection.close.assert_called_once()
        connection = Mock()
        with patch.object(codex_daemon_broker.socket, "socket", return_value=connection), \
                patch.object(codex_daemon_broker, "_send_frame"), \
                patch.object(codex_daemon_broker, "_receive_frame", side_effect=ValueError("invalid frame")):
            self.assertIn("invalid response refused", broker._call_owner({})["error"])
        connection.close.assert_called()

    def test_broker_read_deadline_covers_header_and_body_despite_progress(self):
        connection = Mock()
        payload = b'{}'
        header = codex_daemon_broker._FRAME_HEADER.pack(len(payload))
        connection.recv.side_effect = [header, payload]
        started = time.monotonic()
        with patch.object(codex_daemon_broker.time, "monotonic", side_effect=[started, started + ADMISSION_TEST_WAIT_S]):
            with self.assertRaisesRegex(TimeoutError, "request-read deadline"):
                codex_daemon_broker._receive_frame(connection, started + ADMISSION_TEST_READ_S)
        self.assertEqual(connection.recv.call_count, 1)

    def test_pool_watchdog_default_recovers_hung_live_worker_and_honors_optout(self):
        released = threading.Event()
        recovered = threading.Event()
        workers = []

        def admission_watchdog_factory():
            worker = SimpleNamespace(run_warm=Mock(return_value=dict(ADMISSION_TEST_REPLY)),
                                     ensure_running=Mock(return_value=object()), shutdown=Mock(),
                                     running=Mock(return_value=True))
            if not workers:
                worker.run_warm.side_effect = lambda **kwargs: released.wait(ADMISSION_TEST_WAIT_S)
                worker.shutdown.side_effect = released.set
            else:
                worker.ensure_running.side_effect = lambda: recovered.set() or object()
            workers.append(worker)
            return worker

        with patch.dict(os.environ, {}, clear=True), patch.object(config, "_cfg", return_value={}):
            pool = codex_daemon._CodexDaemonPool(admission_watchdog_factory, size=ADMISSION_TEST_POOL_SIZE)
            try:
                result = pool.run_warm(ADMISSION_TEST_PROMPT, timeout=ADMISSION_TEST_TURN_S)
                self.assertIn("deadline", result["error"])
                self.assertTrue(recovered.wait(ADMISSION_TEST_WAIT_S))
                self.assertTrue(workers[0].shutdown.called)
                self.assertEqual(pool.run_warm(ADMISSION_TEST_PROMPT), ADMISSION_TEST_REPLY)
                self.assertIs(workers[-1].run_warm.call_args.kwargs["recycle_on_timeout"], True)
                with patch.dict(os.environ, {"SPENDGUARD_CODEX_DAEMON_RECYCLE_ON_TIMEOUT": "false"}):
                    self.assertEqual(pool.run_warm(ADMISSION_TEST_PROMPT), ADMISSION_TEST_REPLY)
                    self.assertIs(workers[-1].run_warm.call_args.kwargs["recycle_on_timeout"], False)
                pool.run_warm(ADMISSION_TEST_PROMPT, recycle_on_timeout=False)
                self.assertIs(workers[-1].run_warm.call_args.kwargs["recycle_on_timeout"], False)
            finally:
                released.set()
                pool.shutdown()


if __name__ == "__main__":
    unittest.main()

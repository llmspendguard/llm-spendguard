"""Offline guards for exclusive warm Codex worker checkout and isolated recovery."""
import concurrent.futures
import os
import pathlib
import sys
import tempfile
import threading
from types import SimpleNamespace
from unittest.mock import Mock, patch

os.environ.setdefault("SPENDGUARD_HOME", tempfile.mkdtemp(prefix="sg-codex-pool-"))
os.environ.setdefault("SPENDGUARD_TEST_ISOLATED", "1")
os.environ.setdefault("SPENDGUARD_NO_AUTOINSTALL", "1")
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from spendguard import codex_daemon
from spendguard import codex_exec, config

POOL_GUARD_SIZE = 3
POOL_EVENT_TIMEOUT_S = 10
POOL_QUEUE_TIMEOUT_S = 0.02
POOL_TRANSPORT_TIMEOUT_S = 0.5
POOL_TEST_PROMPT = "pool-guard"
POOL_SUCCESS_REPLY = {"text": "pool reply", "thread": "pool thread", "error": None}
POOL_AUTH_FIXTURE = b'{"auth_mode":"chatgpt","tokens":{}}'
POOL_CONFIG_FIXTURE = 'model = "fixture-model"\n[features]\nexample = false\n'


def _pool_guard_fake_child(turn_handler=None):
    return SimpleNamespace(
        run_warm=Mock(side_effect=turn_handler, return_value=dict(POOL_SUCCESS_REPLY)),
        ensure_running=Mock(return_value=object()), running=Mock(return_value=True),
        shutdown=Mock(), _cancel_idle_shutdown=Mock(), _schedule_idle_shutdown=Mock())


def _pool_guard_wait_recovery(pool):
    with pool._pool_lock:
        recoveries = list(pool._pool_recoveries)
    for recovery in recoveries:
        recovery.join(timeout=POOL_EVENT_TIMEOUT_S)
        assert not recovery.is_alive(), "worker recovery failed to return its lease"


def test_pool_parallel_turns_use_distinct_children_with_real_overlap():
    rendezvous = threading.Barrier(POOL_GUARD_SIZE)
    worker_identities = set()
    identities_lock = threading.Lock()
    workers = []

    def pool_parallel_factory():
        worker = _pool_guard_fake_child()

        def pool_parallel_turn(*args, **kwargs):
            with identities_lock:
                worker_identities.add(id(worker))
            rendezvous.wait(timeout=POOL_EVENT_TIMEOUT_S)
            return dict(POOL_SUCCESS_REPLY)

        worker.run_warm.side_effect = pool_parallel_turn
        workers.append(worker)
        return worker

    pool = codex_daemon._CodexDaemonPool(pool_parallel_factory, size=POOL_GUARD_SIZE)
    try:
        pool.ensure_running()
        assert len(workers) == POOL_GUARD_SIZE
        assert all(worker.ensure_running.called for worker in workers)
        with concurrent.futures.ThreadPoolExecutor(max_workers=POOL_GUARD_SIZE) as executor:
            futures = [executor.submit(pool.run_warm, prompt=POOL_TEST_PROMPT) for _ in range(POOL_GUARD_SIZE)]
            assert all(future.result(timeout=POOL_EVENT_TIMEOUT_S) == POOL_SUCCESS_REPLY for future in futures)
        assert len(worker_identities) == POOL_GUARD_SIZE
    finally:
        pool.shutdown()


def test_pool_wedged_child_recycles_without_touching_other_children():
    entered = threading.Event()
    release_timeout = threading.Event()
    workers = []

    def pool_wedge_turn(*args, **kwargs):
        entered.set()
        assert release_timeout.wait(POOL_EVENT_TIMEOUT_S)
        return {"text": None, "thread": None, "error": "deadline expired", "timed_out": True}

    def pool_wedge_factory():
        worker = _pool_guard_fake_child(pool_wedge_turn if not workers else None)
        workers.append(worker)
        return worker

    pool = codex_daemon._CodexDaemonPool(pool_wedge_factory, size=POOL_GUARD_SIZE)
    try:
        pool.ensure_running()
        with concurrent.futures.ThreadPoolExecutor(max_workers=POOL_GUARD_SIZE) as executor:
            wedged = executor.submit(pool.run_warm, prompt=POOL_TEST_PROMPT)
            assert entered.wait(POOL_EVENT_TIMEOUT_S)
            healthy = executor.submit(pool.run_warm, prompt=POOL_TEST_PROMPT)
            assert healthy.result(timeout=POOL_EVENT_TIMEOUT_S) == POOL_SUCCESS_REPLY
            assert not workers[0].shutdown.called
            release_timeout.set()
            wedged.result(timeout=POOL_EVENT_TIMEOUT_S)
        _pool_guard_wait_recovery(pool)
        assert workers[0].shutdown.called
        assert all(not worker.shutdown.called for worker in workers[1:POOL_GUARD_SIZE])
        assert pool.run_warm(prompt=POOL_TEST_PROMPT) == POOL_SUCCESS_REPLY
    finally:
        release_timeout.set()
        pool.shutdown()


def test_pool_auth_error_recycles_child_and_fails_over_to_healthy_child():
    workers = []
    recovery_entered = threading.Event()
    recovery_release = threading.Event()

    def pool_auth_factory():
        worker = _pool_guard_fake_child()
        if not workers:
            worker.run_warm.return_value = {
                "text": None, "thread": None, "error": "credential rejected", "auth_error": True}
        workers.append(worker)
        return worker

    pool = codex_daemon._CodexDaemonPool(pool_auth_factory, size=POOL_GUARD_SIZE)
    try:
        pool.ensure_running()

        def pool_auth_stalled_restart():
            recovery_entered.set()
            assert recovery_release.wait(POOL_EVENT_TIMEOUT_S)
            return object()

        workers[0].ensure_running.side_effect = pool_auth_stalled_restart
        assert pool.run_warm(prompt=POOL_TEST_PROMPT) == POOL_SUCCESS_REPLY
        assert recovery_entered.wait(POOL_EVENT_TIMEOUT_S)
        assert not recovery_release.is_set()
        recovery_release.set()
        _pool_guard_wait_recovery(pool)
        assert workers[0].shutdown.called
        assert any(worker.run_warm.called for worker in workers[1:POOL_GUARD_SIZE])
        assert all(not worker.shutdown.called for worker in workers[1:POOL_GUARD_SIZE])
    finally:
        recovery_release.set()
        _pool_guard_wait_recovery(pool)
        pool.shutdown()


def test_pool_size_one_preserves_single_child_result_and_arguments_exactly():
    worker = _pool_guard_fake_child()
    options = {"model": "fixture-model", "thread": "fixture-thread", "reasoning": "low",
               "sandbox": "read-only", "timeout": POOL_EVENT_TIMEOUT_S, "recycle_on_timeout": False}
    with patch.dict(os.environ, {"SPENDGUARD_CODEX_DAEMON_POOL_SIZE": "1"}):
        pool = codex_daemon._CodexDaemonPool(lambda: worker)
        try:
            result = pool.run_warm(prompt=POOL_TEST_PROMPT, **options)
            assert result is worker.run_warm.return_value
            worker.run_warm.assert_called_once_with(prompt=POOL_TEST_PROMPT, **options)
            assert len(pool._workers) == 1
        finally:
            pool.shutdown()


def test_pool_shared_and_isolated_homes_preserve_environment_and_private_copies():
    with tempfile.TemporaryDirectory(prefix="sg-pool-home-") as temporary:
        fixture_root = pathlib.Path(temporary)
        source_home = fixture_root / "source"
        source_home.mkdir()
        (source_home / config.CODEX_POOL_AUTH_FILENAME).write_bytes(POOL_AUTH_FIXTURE)
        (source_home / config.CODEX_POOL_CONFIG_FILENAME).write_text(POOL_CONFIG_FIXTURE)
        common_environment = {
            "SPENDGUARD_CODEX_DAEMON_POOL_HOME_ROOT": str(fixture_root / "workers"),
            "SPENDGUARD_CODEX_DAEMON_POOL_SIZE": str(POOL_GUARD_SIZE),
        }
        with patch.dict(os.environ, common_environment), patch.object(
                codex_exec, "_codex_home", return_value=str(source_home)):
            for isolated in (False, True):
                with patch.dict(os.environ, {
                        "SPENDGUARD_CODEX_DAEMON_POOL_ISOLATE_HOMES": str(isolated).lower()}):
                    pool = codex_daemon._CodexDaemonPool(codex_daemon._CodexDaemon)
                    try:
                        worker_homes = set()
                        for worker in pool._workers:
                            assert worker._keep_warm
                            with patch("spendguard.warm_stdio_daemon.threading.Timer") as idle_timer:
                                worker._schedule_idle_shutdown()
                                idle_timer.assert_not_called()
                            overrides = getattr(worker, "_environment_overrides", {})
                            launcher = str(pathlib.Path(sys.executable).resolve())
                            environment_without_codex_home = dict(os.environ)
                            environment_without_codex_home.pop("CODEX_HOME", None)
                            with patch.dict(os.environ, environment_without_codex_home, clear=True), patch.object(
                                    worker, "_spawn_cmd", return_value=[launcher]), patch(
                                    "spendguard.warm_stdio_daemon.subprocess.Popen", side_effect=OSError
                                    ) as spawn_attempt:
                                assert worker._spawn() is None
                            child_env = spawn_attempt.call_args.kwargs["env"]
                            assert child_env["HOME"] == os.environ["HOME"]
                            if not isolated:
                                assert "CODEX_HOME" not in overrides
                                assert "CODEX_HOME" not in child_env
                                continue
                            home = pathlib.Path(overrides["CODEX_HOME"])
                            assert child_env["CODEX_HOME"] == str(home)
                            assert home.is_absolute()
                            worker_homes.add(home)
                            assert "HOME" not in overrides
                            auth_path = home / config.CODEX_POOL_AUTH_FILENAME
                            config_path = home / config.CODEX_POOL_CONFIG_FILENAME
                            assert auth_path.read_bytes() == POOL_AUTH_FIXTURE
                            assert auth_path.stat().st_mode & 0o777 == config.CODEX_POOL_PRIVATE_FILE_MODE
                            assert config_path.stat().st_mode & 0o777 == config.CODEX_POOL_PRIVATE_FILE_MODE
                            import tomllib
                            copied_config = tomllib.loads(config_path.read_text())
                            source_config = tomllib.loads(POOL_CONFIG_FIXTURE)
                            assert copied_config.pop(config.CODEX_POOL_CREDENTIAL_STORE_KEY) == (
                                config.CODEX_POOL_CREDENTIAL_STORE_VALUE)
                            assert copied_config == source_config
                        assert len(worker_homes) == (POOL_GUARD_SIZE if isolated else 0)
                    finally:
                        pool.shutdown()


def test_pool_all_busy_wait_is_bounded_and_raised_worker_releases_slot():
    entered = threading.Barrier(POOL_GUARD_SIZE + 1)
    released = threading.Event()

    def pool_busy_turn(*args, **kwargs):
        entered.wait(timeout=POOL_EVENT_TIMEOUT_S)
        assert released.wait(POOL_EVENT_TIMEOUT_S)
        return dict(POOL_SUCCESS_REPLY)

    pool = codex_daemon._CodexDaemonPool(
        lambda: _pool_guard_fake_child(pool_busy_turn), size=POOL_GUARD_SIZE)
    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=POOL_GUARD_SIZE) as executor:
            turns = [executor.submit(pool.run_warm, prompt=POOL_TEST_PROMPT)
                     for _ in range(POOL_GUARD_SIZE)]
            entered.wait(timeout=POOL_EVENT_TIMEOUT_S)
            blocked_result = pool.run_warm(prompt=POOL_TEST_PROMPT, timeout=POOL_QUEUE_TIMEOUT_S)
            assert blocked_result["error"]
            assert blocked_result["text"] is None
            released.set()
            assert all(turn.result(timeout=POOL_EVENT_TIMEOUT_S) == POOL_SUCCESS_REPLY for turn in turns)
        for worker in pool._workers:
            worker.run_warm.side_effect = RuntimeError("fixture worker failure")
        try:
            pool.run_warm(prompt=POOL_TEST_PROMPT)
        except RuntimeError:
            pass
        else:
            raise AssertionError("worker exception was swallowed")
        _pool_guard_wait_recovery(pool)
        for worker in pool._workers:
            worker.run_warm.side_effect = None
        assert pool.run_warm(prompt=POOL_TEST_PROMPT) == POOL_SUCCESS_REPLY
    finally:
        released.set()
        pool.shutdown()


def test_pool_auth_metadata_parses_protocol_and_actual_rpc_failure_paths():
    auth_error = {"message": "opaque protocol failure", "codexErrorInfo": {
        "httpConnectionFailed": {"httpStatusCode": codex_daemon._CODEX_AUTH_HTTP_STATUSES[0]}}}
    assert codex_daemon._codex_rpc_auth_failure(auth_error)
    assert not codex_daemon._codex_rpc_auth_failure({"message": "unauthorized authentication failed"})
    for failing_phase in ("thread/start", "turn/start", "turn/completed"):
        worker = codex_daemon._CodexDaemon()

        def pool_rpc_reply(process, message, worker=worker, failing_phase=failing_phase):
            waiter = worker._waiters[message["id"]]
            if message["method"] == failing_phase:
                response = {"id": message["id"], "error": auth_error}
            elif message["method"] == "thread/start":
                response = {"id": message["id"], "result": {"thread": {"id": "fixture-thread"}}}
            else:
                response = {"id": message["id"], "result": {"turn": {"id": "fixture-turn"}}}
                worker._on_response(waiter, response)
                worker._route_notification({"method": "turn/completed", "params": {
                    "threadId": "fixture-thread", "turn": {
                        "id": "fixture-turn", "status": "failed", "error": auth_error}}})
            waiter["msg"] = response
            waiter["event"].set()

        with patch.object(worker, "ensure_running", return_value=object()), patch.object(
                worker, "_write_rpc", side_effect=pool_rpc_reply), patch.object(
                worker, "_schedule_idle_shutdown"):
            result = worker.run_warm(POOL_TEST_PROMPT, timeout=POOL_EVENT_TIMEOUT_S)
        assert result["auth_error"] is True, (failing_phase, result)
        assert result["text"] is None


def test_pool_transport_timeout_recycles_only_the_unresponsive_real_worker():
    entered = threading.Event()
    workers = []

    def pool_transport_never_reply(process, message):
        entered.set()

    def pool_transport_factory():
        if workers:
            worker = _pool_guard_fake_child()
        else:
            worker = codex_daemon._CodexDaemon()
            worker.ensure_running = Mock(return_value=object())
            worker._write_rpc = Mock(side_effect=pool_transport_never_reply)
            worker.shutdown = Mock()
        workers.append(worker)
        return worker

    pool = codex_daemon._CodexDaemonPool(pool_transport_factory, size=POOL_GUARD_SIZE)
    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=POOL_GUARD_SIZE) as executor:
            unresponsive = executor.submit(
                pool.run_warm, prompt=POOL_TEST_PROMPT, timeout=POOL_TRANSPORT_TIMEOUT_S)
            assert entered.wait(POOL_EVENT_TIMEOUT_S)
            healthy = executor.submit(pool.run_warm, prompt=POOL_TEST_PROMPT)
            assert healthy.result(timeout=POOL_EVENT_TIMEOUT_S) == POOL_SUCCESS_REPLY
            timeout_result = unresponsive.result(timeout=POOL_EVENT_TIMEOUT_S)
        assert timeout_result["error"]
        assert timeout_result["text"] is None
        _pool_guard_wait_recovery(pool)
        assert workers[0].shutdown.called
        assert all(not worker.shutdown.called for worker in workers[1:])
    finally:
        pool.shutdown()


if __name__ == "__main__":
    for pool_guard_test in (
            test_pool_parallel_turns_use_distinct_children_with_real_overlap,
            test_pool_wedged_child_recycles_without_touching_other_children,
            test_pool_auth_error_recycles_child_and_fails_over_to_healthy_child,
            test_pool_size_one_preserves_single_child_result_and_arguments_exactly,
            test_pool_shared_and_isolated_homes_preserve_environment_and_private_copies,
            test_pool_all_busy_wait_is_bounded_and_raised_worker_releases_slot,
            test_pool_auth_metadata_parses_protocol_and_actual_rpc_failure_paths,
            test_pool_transport_timeout_recycles_only_the_unresponsive_real_worker):
        pool_guard_test()
        print("PASS", pool_guard_test.__name__)

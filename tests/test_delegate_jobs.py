"""Offline durability, handoff, dedup, and compatibility contracts for delegate jobs."""
import contextlib
import datetime
import io
import json
import os
import pathlib
import sys
import tempfile
import threading

TEST_SPENDGUARD_HOME = tempfile.mkdtemp(prefix="sg-delegate-jobs-")
os.environ["SPENDGUARD_HOME"] = TEST_SPENDGUARD_HOME
os.environ.setdefault("SPENDGUARD_TEST_ISOLATED", "1")
os.environ.setdefault("SPENDGUARD_NO_AUTOINSTALL", "1")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from spendguard import delegate_jobs, delegate_router, mcp_server  # noqa: E402

delegate_jobs.config.HOME = pathlib.Path(TEST_SPENDGUARD_HOME)
failures = []
TEST_HANDOFF_WINDOW_S = 0.02
TEST_HANDOFF_POLL_S = 0.001
TEST_WORKER_COMPLETION_WAIT_S = 1.0
TEST_STALE_AGE_DAYS = 1
TEST_STALE_TIMEOUT_S = 0


def check_delegate_job_contract(label, condition):
    passed = bool(condition)
    print(f"  [{'OK' if passed else 'FAIL'}] {label}")
    if not passed:
        failures.append(label)


print("-- durable store roundtrip preserves the whole authoritative payload --")
payload = {"status": "executed", "execution": {"text": "complete execution text"},
           "changed_files": ["src/one.py", "tests/test_one.py"],
           "receipt": {"plan": "fixture-plan", "est_value_basis": "full receipt fixture"},
           "classification": {"kind": "agentic"}, "saving": {"saved": 1}}
job_id = delegate_jobs.create_delegate_job(
    kind="agentic", task_fingerprint="roundtrip-fingerprint", cwd=os.getcwd(),
    provider="fixture-provider", intent="fixture-intent", dirty_before={"existing.py"})
delegate_jobs.finish_delegate_job(job_id, state="done", result_payload=payload)
check_delegate_job_contract("finished row reports done", delegate_jobs.read_delegate_job(job_id)["state"] == "done")
check_delegate_job_contract(
    "whole changed-files list and full receipt survive roundtrip",
    delegate_jobs.read_delegate_job_result(job_id) == {**payload, "state": "done"})

print("-- scalar terminal payload remains collectible through the CLI --")
scalar_delegate_result_payload = "complete scalar delegate result"
scalar_delegate_result_job_id = delegate_jobs.create_delegate_job(
    kind="agentic", task_fingerprint="scalar-result-fingerprint", cwd=os.getcwd(),
    provider="fixture-provider", intent="fixture-intent", dirty_before=set())
delegate_jobs.finish_delegate_job(
    scalar_delegate_result_job_id, state="done", result_payload=scalar_delegate_result_payload)
scalar_delegate_result_stdout = io.StringIO()
with contextlib.redirect_stdout(scalar_delegate_result_stdout):
    scalar_delegate_result_exit_code = delegate_router.delegate_cli(
        ["--collect", scalar_delegate_result_job_id])
check_delegate_job_contract(
    "scalar collect payload prints unchanged and returns success",
    scalar_delegate_result_exit_code == 0
    and json.loads(scalar_delegate_result_stdout.getvalue()) == scalar_delegate_result_payload)

print("-- start returns before worker completion and another connection reads the result --")
thread_payload = {**payload, "execution": {"text": "thread-owned durable result"}}
worker_finished = threading.Event()
original_start_delegate_task = delegate_router.delegate_task
original_worktree_state = delegate_router._worktree_state
original_run_to_completion = delegate_router._run_delegate_job_to_completion
delegate_router.delegate_task = lambda *args, **kwargs: {
    "status": "estimate", "classification": {"kind": "agentic"},
    "estimate": {"plan": "fixture-provider", "model": "fixture-model"},
}
delegate_router._worktree_state = lambda cwd: ("fixture-status", set())


def finish_started_delegate_fixture(started_job_id, *args, **kwargs):
    delegate_jobs.finish_delegate_job(started_job_id, state="done", result_payload=thread_payload)
    worker_finished.set()


delegate_router._run_delegate_job_to_completion = finish_started_delegate_fixture
try:
    worker_job = delegate_router.start_delegate_job(
        "thread task", provider="fixture-provider", cwd=os.getcwd())
    worker_finished.wait(TEST_WORKER_COMPLETION_WAIT_S)
finally:
    delegate_router.delegate_task = original_start_delegate_task
    delegate_router._worktree_state = original_worktree_state
    delegate_router._run_delegate_job_to_completion = original_run_to_completion
check_delegate_job_contract(
    "start returned a handle and separate post-return read sees worker result",
    worker_job["state"] == "running"
    and delegate_jobs.read_delegate_job_result(worker_job["job_id"]) == {**thread_payload, "state": "done"})

print("-- stale running rows fail closed instead of remaining perpetual --")
stale_job_id = delegate_jobs.create_delegate_job(
    kind="agentic", task_fingerprint="stale-fingerprint", cwd=os.getcwd(),
    provider="fixture-provider", intent="fixture-intent", dirty_before=set())
stale_timestamp = (
    datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=TEST_STALE_AGE_DAYS)
).isoformat(timespec="seconds")
with delegate_jobs._open_delegate_jobs_connection() as stale_connection:
    stale_connection.execute("UPDATE delegate_jobs SET updated_ts=? WHERE job_id=?", (stale_timestamp, stale_job_id))
original_cfg_get = delegate_jobs.config._cfg_get
original_margin = delegate_jobs.DELEGATE_JOB_STALENESS_MARGIN_S
delegate_jobs.config._cfg_get = lambda section, key, default=None: (
    TEST_STALE_TIMEOUT_S if key == "agentic_timeout_s" else original_cfg_get(section, key, default)
)
delegate_jobs.DELEGATE_JOB_STALENESS_MARGIN_S = TEST_STALE_TIMEOUT_S
try:
    stale_status = delegate_jobs.read_delegate_job(stale_job_id)
finally:
    delegate_jobs.config._cfg_get = original_cfg_get
    delegate_jobs.DELEGATE_JOB_STALENESS_MARGIN_S = original_margin
check_delegate_job_contract(
    "stale running row reports restart-unknown failure",
    stale_status["state"] == "failed" and stale_status["terminal_reason"] == "server-restart-unknown")

print("-- bounded handoff returns inline when fast and a collectible handle when slow --")
original_handoff = delegate_router.DELEGATE_HANDOFF_AFTER_S
original_poll = delegate_router.DELEGATE_HANDOFF_POLL_S
delegate_router.DELEGATE_HANDOFF_AFTER_S = TEST_HANDOFF_WINDOW_S
delegate_router.DELEGATE_HANDOFF_POLL_S = TEST_HANDOFF_POLL_S
fast_job_id = delegate_jobs.create_delegate_job(
    kind="agentic", task_fingerprint="fast-fingerprint", cwd=os.getcwd(),
    provider="fixture-provider", intent="fixture-intent", dirty_before=set())
delegate_jobs.finish_delegate_job(fast_job_id, state="done", result_payload=payload)
check_delegate_job_contract(
    "fast completion returns unchanged inline shape",
    delegate_router.wait_for_delegate_job_handoff(fast_job_id) == {**payload, "state": "done"})
slow_job_id = delegate_jobs.create_delegate_job(
    kind="agentic", task_fingerprint="slow-fingerprint", cwd=os.getcwd(),
    provider="fixture-provider", intent="fixture-intent", dirty_before=set())
slow_handle = delegate_router.wait_for_delegate_job_handoff(slow_job_id)
check_delegate_job_contract(
    "slow completion returns running job handle and collect command",
    slow_handle["status"] == "running" and slow_handle["job_id"] == slow_job_id
    and slow_job_id in slow_handle["collect_with"])
delegate_jobs.finish_delegate_job(slow_job_id, state="done", result_payload=thread_payload)
check_delegate_job_contract(
    "MCP result collector returns the later terminal payload",
    mcp_server._tool_delegate_result({"job_id": slow_job_id}) == {**thread_payload, "state": "done"})
delegate_router.DELEGATE_HANDOFF_AFTER_S = original_handoff
delegate_router.DELEGATE_HANDOFF_POLL_S = original_poll

print("-- MCP deduplicates identical in-flight agentic submissions --")


def check_duplicate_submit_contract():
    original_classify = delegate_router.classify_task
    original_fingerprint = delegate_router.delegate_route_fingerprint
    original_start = delegate_router.start_delegate_job
    original_wait = delegate_router.wait_for_delegate_job_handoff
    start_calls = []
    delegate_router.classify_task = lambda *args, **kwargs: {"kind": "agentic"}
    delegate_router.delegate_route_fingerprint = lambda *args, **kwargs: "mcp-dedup-fingerprint"

    def start_mcp_delegate_fixture(*args, **kwargs):
        start_calls.append((args, kwargs))
        created_id = delegate_jobs.create_delegate_job(
            kind="agentic", task_fingerprint="mcp-dedup-fingerprint", cwd=kwargs["cwd"],
            provider="fixture-provider", intent="fixture-intent", dirty_before=set())
        return {"job_id": created_id, "state": "running"}

    delegate_router.start_delegate_job = start_mcp_delegate_fixture
    delegate_router.wait_for_delegate_job_handoff = lambda current_job_id: {"status": "running", "job_id": current_job_id}
    try:
        first = mcp_server._tool_delegate({"task": "same task", "execute": True, "cwd": os.getcwd()})
        second = mcp_server._tool_delegate({"task": "same task", "execute": True, "cwd": os.getcwd()})
    finally:
        delegate_router.classify_task = original_classify
        delegate_router.delegate_route_fingerprint = original_fingerprint
        delegate_router.start_delegate_job = original_start
        delegate_router.wait_for_delegate_job_handoff = original_wait
    check_delegate_job_contract(
        "duplicate submit returns one job id and starts executor once",
        first["job_id"] == second["job_id"] and len(start_calls) == 1)


check_duplicate_submit_contract()

print("-- dry-run and non-agentic execute remain synchronous and create no jobs --")


def check_synchronous_delegate_contract():
    original_delegate_task = delegate_router.delegate_task
    original_classify = delegate_router.classify_task
    sync_calls = []

    def synchronous_delegate_fixture(task, **kwargs):
        sync_calls.append((task, kwargs))
        return {"status": "executed" if kwargs["execute"] else "estimate", "classification": {"kind": "oneshot"}}

    delegate_router.delegate_task = synchronous_delegate_fixture
    delegate_router.classify_task = lambda *args, **kwargs: {"kind": "oneshot"}
    try:
        dry_result = mcp_server._tool_delegate({"task": "dry", "execute": False})
        oneshot_result = mcp_server._tool_delegate({"task": "oneshot", "execute": True})
    finally:
        delegate_router.delegate_task = original_delegate_task
        delegate_router.classify_task = original_classify
    check_delegate_job_contract(
        "dry-run estimate and non-agentic execution retain inline status shapes",
        dry_result["status"] == "estimate" and oneshot_result["status"] == "executed"
        and [call[1]["execute"] for call in sync_calls] == [False, True])


check_synchronous_delegate_contract()

print("-- cold and unreadable delegate stores remain distinguishable --")
original_database_path = delegate_jobs.delegate_jobs_database_path
original_readonly_connection = delegate_jobs._open_delegate_jobs_readonly_connection
with tempfile.TemporaryDirectory(prefix="sg-delegate-jobs-cold-") as cold_store_directory:
    cold_store_path = pathlib.Path(cold_store_directory) / delegate_jobs.DELEGATE_JOBS_DATABASE_FILENAME
    delegate_jobs.delegate_jobs_database_path = lambda: cold_store_path
    try:
        cold_store_status = delegate_jobs.read_delegate_job("cold-store-job")
        cold_store_inflight = delegate_jobs.find_inflight_delegate_job("cold-store-fingerprint", cold_store_directory)
    finally:
        delegate_jobs.delegate_jobs_database_path = original_database_path
check_delegate_job_contract(
    "cold store reports not-found rather than indeterminate",
    cold_store_status == {"error": "not-found", "job_id": "cold-store-job"})
check_delegate_job_contract(
    "cold store has no in-flight delegate job",
    cold_store_inflight is None)

with tempfile.TemporaryDirectory(prefix="sg-delegate-jobs-unreadable-") as unreadable_store_directory:
    unreadable_store_path = pathlib.Path(unreadable_store_directory) / delegate_jobs.DELEGATE_JOBS_DATABASE_FILENAME
    unreadable_store_path.touch()
    delegate_jobs.delegate_jobs_database_path = lambda: unreadable_store_path
    delegate_jobs._open_delegate_jobs_readonly_connection = lambda: (_ for _ in ()).throw(
        delegate_jobs.sqlite3.OperationalError("unreadable delegate-jobs fixture"))
    try:
        unreadable_store_status = delegate_jobs.read_delegate_job("unreadable-store-job")
        unreadable_store_result = delegate_jobs.read_delegate_job_result("unreadable-store-job")
        try:
            delegate_jobs.find_inflight_delegate_job("unreadable-store-fingerprint", unreadable_store_directory)
        except delegate_jobs.DelegateJobStoreUnreadable:
            unreadable_store_dedup_failed_closed = True
        else:
            unreadable_store_dedup_failed_closed = False
    finally:
        delegate_jobs.delegate_jobs_database_path = original_database_path
        delegate_jobs._open_delegate_jobs_readonly_connection = original_readonly_connection
check_delegate_job_contract(
    "unreadable store status is indeterminate with named reason",
    unreadable_store_status["state"] == "indeterminate"
    and unreadable_store_status["terminal_reason"] == delegate_jobs.DELEGATE_JOB_STORE_UNREADABLE_REASON)
check_delegate_job_contract(
    "unreadable store result is indeterminate with named reason",
    unreadable_store_result["status"] == "indeterminate"
    and unreadable_store_result["terminal_reason"] == delegate_jobs.DELEGATE_JOB_STORE_UNREADABLE_REASON)
check_delegate_job_contract(
    "dedup raises and fails closed when delegate store is unreadable",
    unreadable_store_dedup_failed_closed)

if failures:
    raise SystemExit(f"delegate job contract failures: {json.dumps(failures)}")
print("delegate job contracts: all checks passed")

"""Recoverable dispatch evidence and an exactly-once terminal journal.

Raw evidence lives in durable files, independently of any result envelope. A
detached observer closes executions whose owning process disappears.
"""
import contextlib
import contextvars
import datetime
import fcntl
import functools
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import uuid

EXECUTION_STORE_DIRECTORY = "delegate-executions"
EXECUTION_WATCH_INTERVAL_DEFAULT_S = 0.1
EXECUTION_STATE_FILENAME = "state.json"
EXECUTION_EVENTS_FILENAME = "events.jsonl"
EXECUTION_LOCK_FILENAME = "execution.lock"
EXECUTION_PAYLOAD_FILENAMES = {"result": "result.txt", "stdout": "stdout.txt", "stderr": "stderr.txt",
                               "envelope": "envelope.json"}
EXECUTION_TERMINAL_STATES = ("success", "failure", "killed", "lost")
EXECUTION_OBSERVER_READY_FILENAME = "observer.ready"
EXECUTION_OBSERVER_STDERR_FILENAME = "observer.stderr"
EXECUTION_OBSERVER_START_BOUND_DEFAULT_S = 10
EXECUTION_PARTIAL_EVENT_ARCHIVE_SUFFIX = ".interrupted"
_durable_dispatch_context = contextvars.ContextVar("spendguard_durable_dispatch", default=None)


class DurableExecutionPersistenceError(RuntimeError):
    """Evidence could not be safely persisted; never reinterpret as provider failure."""


def _protect_durable_execution_io(operation):
    @functools.wraps(operation)
    def guarded_execution_io(*args, **kwargs):
        try:
            return operation(*args, **kwargs)
        except OSError as error:
            raise DurableExecutionPersistenceError(f"{operation.__name__}: evidence persistence failed: {error}") from error
    return guarded_execution_io


def _read_durable_execution_event_lines(stream, path):
    stream.seek(0)
    raw = stream.read()
    rows = []
    for index, line in enumerate(raw.splitlines()):
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            if index != len(raw.splitlines()) - 1:
                raise
            # Preserve the entire interrupted file before repairing the append boundary.
            archive = path.with_name(path.name + EXECUTION_PARTIAL_EVENT_ARCHIVE_SUFFIX + uuid.uuid4().hex)
            with archive.open("w", encoding="utf-8") as evidence:
                evidence.write(raw)
                evidence.flush()
                os.fsync(evidence.fileno())
            stream.seek(0)
            stream.truncate()
            stream.write("".join(json.dumps(row) + "\n" for row in rows))
    if raw and not raw.endswith("\n") and len(rows) == len(raw.splitlines()):
        stream.seek(0, os.SEEK_END)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    return rows


def durable_execution_store_root():
    from . import config
    configured = os.getenv("SPENDGUARD_EXECUTION_STORE") or config._cfg_get("delegate", "execution_store", None)
    return Path(configured).expanduser() if configured else config.HOME / EXECUTION_STORE_DIRECTORY


def current_durable_execution_id():
    return _durable_dispatch_context.get()


@contextlib.contextmanager
def durable_execution_scope(execution_id):
    token = _durable_dispatch_context.set(execution_id)
    try:
        yield execution_id
    finally:
        _durable_dispatch_context.reset(token)


def _durable_execution_directory(execution_id, root=None):
    if Path(execution_id).name != execution_id:
        raise ValueError("execution id must be a basename")
    return (Path(root) if root is not None else durable_execution_store_root()) / execution_id


@contextlib.contextmanager
def _locked_durable_execution(execution_id, root=None):
    directory = _durable_execution_directory(execution_id, root)
    with (directory / EXECUTION_LOCK_FILENAME).open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield directory


def _write_durable_execution_state(directory, state):
    from . import config
    state_path = directory / EXECUTION_STATE_FILENAME
    config.update_json(state_path, lambda previous: state, required=True,
                       reason="durable execution lifecycle")
    with state_path.open("rb") as stream:
        os.fsync(stream.fileno())
    descriptor = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _append_durable_execution_event(directory, state, phase):
    event = {**state.get("metadata", {}), "execution_id": state["execution_id"], "phase": phase, "event": phase,
             "pid": state["owner_pid"], "state": state["state"],
             "ts": datetime.datetime.now(datetime.timezone.utc).isoformat(),
             "exit_code": state.get("exit_code"), "result_path": str(directory / EXECUTION_PAYLOAD_FILENAMES["result"]),
             "stdout_path": str(directory / EXECUTION_PAYLOAD_FILENAMES["stdout"]),
             "stderr_path": str(directory / EXECUTION_PAYLOAD_FILENAMES["stderr"])}
    paths = [directory / EXECUTION_EVENTS_FILENAME]
    if state.get("metadata", {}).get("ledger_path"):
        paths.append(Path(state["metadata"]["ledger_path"]))
    for path in paths:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a+", encoding="utf-8") as stream:
            fcntl.flock(stream, fcntl.LOCK_EX)
            existing = _read_durable_execution_event_lines(stream, path)
            if any(row.get("execution_id") == state["execution_id"] and
                   row.get("phase", row.get("event")) == phase for row in existing):
                continue
            if not existing and path != directory / EXECUTION_EVENTS_FILENAME:
                stream.write(json.dumps({"event": "coverage_start", "ts": event["ts"]}) + "\n")
            stream.write(json.dumps(event) + "\n")
            stream.flush()
            os.fsync(stream.fileno())


@_protect_durable_execution_io
def begin_durable_execution(metadata=None):
    import psutil
    execution_id = uuid.uuid4().hex
    directory = _durable_execution_directory(execution_id)
    directory.mkdir(parents=True)
    owner = psutil.Process((metadata or {}).get("owner_pid"))
    state = {"execution_id": execution_id, "state": "running", "owner_pid": owner.pid,
             "owner_birth": owner.create_time(), "metadata": metadata or {}, "exit_code": None}
    with _locked_durable_execution(execution_id):
        for filename in EXECUTION_PAYLOAD_FILENAMES.values():
            (directory / filename).touch()
        _write_durable_execution_state(directory, state)
    environment = os.environ.copy()
    source_root = str(Path(__file__).resolve().parent.parent)
    environment["PYTHONPATH"] = os.pathsep.join(filter(None, (source_root, environment.get("PYTHONPATH"))))
    interval = environment.get("SPENDGUARD_EXECUTION_WATCH_INTERVAL_S", str(EXECUTION_WATCH_INTERVAL_DEFAULT_S))
    # stderr remains on disk: observer persistence failures are never discarded.
    with (directory / EXECUTION_OBSERVER_STDERR_FILENAME).open("ab") as diagnostics:
        observer = subprocess.Popen([sys.executable, "-m", __name__, execution_id, str(directory.parent), interval],
                         env=environment, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=diagnostics, start_new_session=True, close_fds=True)
    start_bound = float(environment.get("SPENDGUARD_EXECUTION_OBSERVER_START_BOUND_S",
                                        EXECUTION_OBSERVER_START_BOUND_DEFAULT_S))
    deadline = time.monotonic() + start_bound
    while not (directory / EXECUTION_OBSERVER_READY_FILENAME).exists():
        if observer.poll() is not None or time.monotonic() >= deadline:
            raise DurableExecutionPersistenceError(f"execution {execution_id}: observer startup unconfirmed")
        time.sleep(float(interval))
    with _locked_durable_execution(execution_id):
        _append_durable_execution_event(directory, state, "start")
    return execution_id


def durable_execution_payload_paths(execution_id):
    directory = _durable_execution_directory(execution_id)
    return {key: directory / filename for key, filename in EXECUTION_PAYLOAD_FILENAMES.items()}


@_protect_durable_execution_io
def run_durable_delegate_subprocess(command, **kwargs):
    """Run with OS-level streaming into evidence files whenever dispatch scope is active."""
    execution_id = current_durable_execution_id()
    if execution_id is None:
        return subprocess.run(command, **kwargs)
    paths = durable_execution_payload_paths(execution_id)
    kwargs.pop("capture_output", None)
    kwargs.pop("stdout", None)
    kwargs.pop("stderr", None)
    check = kwargs.pop("check", False)
    timeout = kwargs.pop("timeout", None)
    child_input = kwargs.pop("input", None)
    if child_input is not None:
        kwargs["stdin"] = subprocess.PIPE
    expired = None
    output_offset = paths["stdout"].stat().st_size
    error_offset = paths["stderr"].stat().st_size
    with paths["stdout"].open("ab") as output, paths["stderr"].open("ab") as errors:
        with subprocess.Popen(command, stdout=output, stderr=errors, **kwargs) as child:
            bind_durable_execution_child(execution_id, child.pid)
            try:
                child.communicate(input=child_input, timeout=timeout)
            except subprocess.TimeoutExpired as error:
                child.kill()
                child.wait()
                expired = error
            completed = subprocess.CompletedProcess(command, child.returncode)
        output.flush()
        errors.flush()
        os.fsync(output.fileno())
        os.fsync(errors.fileno())
    persist_durable_execution_payload(execution_id, exit_code=completed.returncode)
    with paths["stdout"].open("rb") as output:
        output.seek(output_offset)
        completed.stdout = output.read().decode("utf-8", errors="surrogateescape")
    with paths["stderr"].open("rb") as errors:
        errors.seek(error_offset)
        completed.stderr = errors.read().decode("utf-8", errors="surrogateescape")
    if expired is not None:
        raise expired
    if check:
        completed.check_returncode()
    return completed


@_protect_durable_execution_io
def persist_durable_execution_payload(execution_id, *, result=None, stdout=None, stderr=None, exit_code=None,
                                      envelope=None, root=None):
    with _locked_durable_execution(execution_id, root) as directory:
        state = json.loads((directory / EXECUTION_STATE_FILENAME).read_text())
        _persist_locked_execution_payload(directory, state, result=result, stdout=stdout, stderr=stderr,
                                          exit_code=exit_code, envelope=envelope)
        _write_durable_execution_state(directory, state)


def _persist_locked_execution_payload(directory, state, *, result=None, stdout=None, stderr=None,
                                      exit_code=None, envelope=None):
    for key, payload in (("result", result), ("stdout", stdout), ("stderr", stderr), ("envelope", envelope)):
        if payload is not None:
            with (directory / EXECUTION_PAYLOAD_FILENAMES[key]).open("w", encoding="utf-8") as stream:
                stream.write(payload if isinstance(payload, str) else json.dumps(payload))
                stream.flush()
                os.fsync(stream.fileno())
    if exit_code is not None:
        state["exit_code"] = exit_code


def append_durable_execution_evidence(execution_id, *, stdout=None, stderr=None):
    try:
        with _locked_durable_execution(execution_id) as directory:
            for key, payload in (("stdout", stdout), ("stderr", stderr)):
                if payload is not None:
                    with (directory / EXECUTION_PAYLOAD_FILENAMES[key]).open("ab") as stream:
                        stream.write(payload if isinstance(payload, bytes) else payload.encode("utf-8"))
                        stream.flush()
                        os.fsync(stream.fileno())
    except OSError as error:
        raise DurableExecutionPersistenceError(f"execution {execution_id} evidence persistence failed") from error


@_protect_durable_execution_io
def terminalize_durable_execution(execution_id, state, *, root=None, **payload):
    """The sole terminal-write seam, serialized across owner and detached observer."""
    if state not in EXECUTION_TERMINAL_STATES:
        raise ValueError(f"invalid execution terminal state: {state}")
    with _locked_durable_execution(execution_id, root) as directory:
        record = json.loads((directory / EXECUTION_STATE_FILENAME).read_text())
        if record["state"] in EXECUTION_TERMINAL_STATES:
            _append_durable_execution_event(directory, record, "end")
            return record
        # Consult the authoritative end before accepting any replacement evidence.
        event_path = directory / EXECUTION_EVENTS_FILENAME
        with event_path.open("a+", encoding="utf-8") as events_stream:
            events = _read_durable_execution_event_lines(events_stream, event_path)
        terminal = next((event for event in events if event["phase"] == "end"), None)
        if terminal is not None:
            record["state"] = terminal["state"]
            record["exit_code"] = terminal.get("exit_code")
        else:
            _persist_locked_execution_payload(directory, record, **payload)
            for filename in EXECUTION_PAYLOAD_FILENAMES.values():
                with (directory / filename).open("rb") as evidence:
                    os.fsync(evidence.fileno())
            record["state"] = ("killed" if state in {"failure", "lost"}
                               and record.get("exit_code") is not None
                               and record["exit_code"] < 0 else state)
        _append_durable_execution_event(directory, record, "end")
        _write_durable_execution_state(directory, record)
        return record


def _durable_execution_owner_alive(record):
    import psutil
    try:
        process = psutil.Process(record["owner_pid"])
        return process.create_time() == record["owner_birth"] and process.status() != psutil.STATUS_ZOMBIE
    except psutil.NoSuchProcess:
        return False


@_protect_durable_execution_io
def bind_durable_execution_child(execution_id, pid):
    import psutil
    try:
        birth = psutil.Process(pid).create_time()
    except psutil.NoSuchProcess:
        return
    with _locked_durable_execution(execution_id) as directory:
        record = json.loads((directory / EXECUTION_STATE_FILENAME).read_text())
        record["child_pid"] = pid
        record["child_birth"] = birth
        _write_durable_execution_state(directory, record)


def _durable_execution_has_live_process(record):
    if _durable_execution_owner_alive(record):
        return True
    if record.get("child_pid"):
        return _durable_execution_owner_alive({"owner_pid": record["child_pid"],
                                                "owner_birth": record["child_birth"]})
    return False


@_protect_durable_execution_io
def reference_durable_execution_stderr(execution_id, archive_path):
    """Reference the complete shared daemon archive without implying exclusive attribution."""
    with _locked_durable_execution(execution_id) as directory:
        record = json.loads((directory / EXECUTION_STATE_FILENAME).read_text())
        references = record.setdefault("shared_daemon_stderr_paths", [])
        absolute_path = os.fspath(Path(archive_path).resolve())
        if absolute_path not in references:
            references.append(absolute_path)
        _write_durable_execution_state(directory, record)


@_protect_durable_execution_io
def recover_durable_execution(execution_id, *, root=None):
    directory = _durable_execution_directory(execution_id, root)
    with _locked_durable_execution(execution_id, root):
        record = json.loads((directory / EXECUTION_STATE_FILENAME).read_text())
    if record["state"] == "running" and not _durable_execution_has_live_process(record):
        record = terminalize_durable_execution(execution_id, "lost", root=root)
    for key, filename in EXECUTION_PAYLOAD_FILENAMES.items():
        record[key] = (directory / filename).read_text(encoding="utf-8", errors="surrogateescape")
    if not record["result"] and record["envelope"]:
        try:
            envelope = json.loads(record["envelope"])
        except json.JSONDecodeError as error:
            record["envelope_parse_error"] = str(error)
        else:
            if isinstance(envelope, dict) and isinstance(envelope.get("text"), str):
                record["result"] = envelope["text"]
                record["result_source"] = "envelope.text"
    for archive in record.get("shared_daemon_stderr_paths", []):
        record["stderr"] += Path(archive).read_text(encoding="utf-8", errors="surrogateescape")
    if record.get("shared_daemon_stderr_paths"):
        record["stderr_attribution"] = "shared_daemon"
    return record


def _observe_durable_execution(execution_id, root, interval):
    directory = _durable_execution_directory(execution_id, root)
    with (directory / EXECUTION_OBSERVER_READY_FILENAME).open("w") as ready:
        ready.flush()
        os.fsync(ready.fileno())
    while True:
        with _locked_durable_execution(execution_id, root):
            record = json.loads((directory / EXECUTION_STATE_FILENAME).read_text())
        metadata = record.get("metadata", {})
        if record["state"] != "running":
            if not metadata.get("job_id"):
                return
            from . import delegate_jobs
            job = delegate_jobs._delegate_job_row(metadata["job_id"])
            if job is not None and job["state"] != delegate_jobs.DELEGATE_JOB_RUNNING_STATE:
                return
            if not _durable_execution_owner_alive(record):
                delegate_jobs.recover_delegate_job_execution(metadata["job_id"])
                return
            time.sleep(interval)
            continue
        if not _durable_execution_has_live_process(record):
            terminalize_durable_execution(execution_id, "lost", root=root)
            metadata = record.get("metadata", {})
            if metadata.get("job_id"):
                from . import delegate_jobs
                delegate_jobs.recover_delegate_job_execution(metadata["job_id"])
            return
        time.sleep(interval)


if __name__ == "__main__":
    _observe_durable_execution(sys.argv[1], sys.argv[2], float(sys.argv[3]))

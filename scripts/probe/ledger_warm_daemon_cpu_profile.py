"""Part A only: fail-closed warm-daemon reproduction and py-spy capture.

The estimate-only consumer call makes no model calls. An OS sandbox denies all
external networking and all filesystem writes except the isolated output tree.
SQLite's online backup API creates an isolated snapshot from a mode=ro source;
the daemon writes only its isolated output tree. Failure to reproduce is a named gap.
"""

import argparse
import atexit
from collections import defaultdict
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import sqlite3
import threading
import time
import runpy
import signal
import re
import weakref
import urllib.error
import urllib.request

MINIMUM_PROFILE_SECONDS = 60
REVIEW_REQUEST_ID = 1
LEDGER_PROFILE_SETUP_IDENTITIES = frozenset({
    ("spendguard/config.py", "tune_ledger_connection"),
    ("spendguard/config.py", "pooled_ledger_conn"),
    ("spendguard/config.py", "fresh_ledger_conn"),
    ("spendguard/ledger.py", "SpendLedger.__init__"),
    ("spendguard/ledger.py", "SpendLedger._ensure_schema"),
})
LEDGER_PROFILE_QUERY_IDENTITIES = frozenset(
    ("spendguard/ledger.py", f"SpendLedger.{method}") for method in (
        "_lock_date", "_audit", "record_event", "update", "lock_period",
        "_clone_for_correction", "reverse", "get", "history", "query",
        "in_category", "rollup", "_cat_dec", "_freshness_token", "sum_by",
        "distinct", "source_rows", "min_day", "count_events", "bulk_retag_project",
        "delete", "sum_dec", "verify_audit_chain",
    )
)
LEDGER_PROFILE_SQL_METHODS = frozenset({
    "execute", "executemany", "executescript", "fetchone", "fetchmany", "fetchall", "__next__",
})
LEDGER_PROFILE_SAMPLE_THREAD_PATTERN = re.compile(r"^\s*\d+\s+Thread_(0x[0-9a-fA-F]+|[0-9]+)\b")
LEDGER_PROFILE_SAMPLE_THREAD_BOUNDARY = re.compile(r"^\s*(?:\d+\s+)?Thread(?:_|\b)")
LEDGER_PROFILE_SAMPLE_STEP_PATTERN = re.compile(r"^[\s+|!:]*\d+\s+sqlite3_step(?:\s|$)")
SOURCE_DIRECTORY_NAME = "src"
MCP_PROTOCOL_VERSION = "2024-11-05"
MCP_PROBE_CLIENT = {"name": "ledger-profile", "version": "1"}
SANDBOX_TEMPLATE = '''(version 1)
(deny default)
(allow process*)
(allow sysctl-read)
(allow mach-lookup)
(allow file-read*)
(allow file-write* (subpath (param "OUTPUT")))
(allow network* (local ip "localhost:{port}") (remote ip "localhost:{port}"))
'''


def ledger_profile_frame_category(frame, source_directory, sql_execution=False):
    """Use scoped identities; unknown source frames remain counted as other."""
    identities = []
    while frame is not None:
        try:
            path = Path(frame.f_code.co_filename).resolve().relative_to(source_directory).as_posix()
        except ValueError:
            frame = frame.f_back
            continue
        identities.append((path, frame.f_code.co_qualname))
        frame = frame.f_back
    if any(identity in LEDGER_PROFILE_SETUP_IDENTITIES for identity in identities):
        return "ledger_setup"
    if sql_execution and identities and identities[0] in LEDGER_PROFILE_QUERY_IDENTITIES:
        return "query"
    return "other"


def ledger_profile_running_step_threads(shape, native):
    """Require the same explicit OS TID in ps running rows and sample stacks."""
    lines = [line.split() for line in shape.splitlines() if line.strip()]
    if not lines or "TID" not in lines[0] or "STAT" not in lines[0]:
        raise RuntimeError("Burst shape unavailable: ps must expose per-thread TID and STAT")
    tid_column, state_column = lines[0].index("TID"), lines[0].index("STAT")
    thread_states = {}
    for fields in lines[1:]:
        try:
            token = fields[tid_column]
            tid = int(token, 16 if token.startswith("0x") else 10)
            state = fields[state_column]
        except (ValueError, IndexError) as error:
            raise RuntimeError("Burst shape unavailable: malformed ps thread row") from error
        if tid in thread_states and thread_states[tid] != state:
            raise RuntimeError("Burst shape unavailable: conflicting states for one TID")
        thread_states[tid] = state
    running = {tid for tid, state in thread_states.items() if state.startswith("R")}
    stepping, sampled = set(), set()
    current_tid = None
    for line in native.splitlines():
        header = LEDGER_PROFILE_SAMPLE_THREAD_PATTERN.match(line)
        if header:
            token = header.group(1)
            current_tid = int(token, 16 if token.startswith("0x") else 10)
            sampled.add(current_tid)
        elif LEDGER_PROFILE_SAMPLE_THREAD_BOUNDARY.match(line):
            raise RuntimeError("Burst shape unavailable: unsupported sample thread header")
        elif line.strip().startswith(("Binary Images:", "Total number in stack")):
            current_tid = None
        if current_tid is not None and LEDGER_PROFILE_SAMPLE_STEP_PATTERN.match(line):
            stepping.add(current_tid)
    if not sampled or not lines[1:]:
        raise RuntimeError("Burst shape unavailable: missing per-thread ps/sample attribution")
    return running & stepping


def ledger_profile_worker():
    """Instrument only this probe child, preserving production connection policy.

    Thread CPU accounting partitions time after installation into setup, SQL C
    execution, and other; it is NOT py-spy sample-count attribution. Profile hook
    overhead is included and reported. Early sitecustomize work is unobserved.
    """
    import spendguard
    from spendguard import config
    spendguard.require()
    source_directory = Path(os.environ["LEDGER_PROFILE_SOURCE"]).resolve()
    if not Path(config.__file__).resolve().is_relative_to(source_directory):
        raise RuntimeError("Gated daemon imported spendguard outside the requested worktree")
    telemetry = Path(os.environ["LEDGER_PROFILE_TELEMETRY"])
    cpu_totals = defaultdict(lambda: defaultdict(float))
    states = threading.local()
    original_connect = sqlite3.connect
    connection_serial = iter(range(sys.maxsize))
    telemetry_lock = threading.Lock()

    def ledger_profile_event(payload):
        with telemetry_lock:
            with telemetry.open("a") as stream:
                stream.write(json.dumps(payload) + "\n")

    def ledger_profile_finalize(serial, born):
        ledger_profile_event({"event": "finalized", "connection": serial,
                              "lifetime_seconds": time.monotonic() - born})

    def ledger_profile_sql_wrapper(operation):
        def ledger_profile_sql_operation(self, *positional, **keyword):
            category = ledger_profile_frame_category(sys._getframe(), source_directory, True)
            outermost = not getattr(states, "sql_depth", 0)
            states.sql_depth = getattr(states, "sql_depth", 0) + 1
            began = time.thread_time()
            try:
                return operation(self, *positional, **keyword)
            finally:
                elapsed = time.thread_time() - began
                states.sql_depth -= 1
                if outermost and category == "query":
                    cpu_totals[threading.get_ident()]["query_native_seconds"] += elapsed
        return ledger_profile_sql_operation

    class LedgerProfileCursor(sqlite3.Cursor):
        pass

    sql_methods = ("execute", "executemany", "executescript", "fetchone", "fetchmany", "fetchall", "__next__")
    for method_name in sql_methods:
        setattr(LedgerProfileCursor, method_name,
                ledger_profile_sql_wrapper(getattr(sqlite3.Cursor, method_name)))

    class LedgerProfileConnection(sqlite3.Connection):
        def cursor(self, factory=LedgerProfileCursor):
            if factory is not LedgerProfileCursor:
                raise RuntimeError("Probe cannot instrument caller-supplied cursor factory")
            return super().cursor(factory)

        def execute(self, *positional, **keyword):
            return self.cursor().execute(*positional, **keyword)

        def executemany(self, *positional, **keyword):
            return self.cursor().executemany(*positional, **keyword)

        def executescript(self, *positional, **keyword):
            return self.cursor().executescript(*positional, **keyword)

        def close(self):
            super().close()
            if self._ledger_profile_finalizer.alive:
                self._ledger_profile_finalizer()

    def ledger_profile_connect(*positional, **keyword):
        # A caller-provided factory is a distinct protocol. Do not silently alter it.
        if "factory" in keyword or len(positional) > 5:
            raise RuntimeError("Probe cannot instrument caller-supplied SQLite factory")
        keyword["factory"] = LedgerProfileConnection
        connection = original_connect(*positional, **keyword)
        serial = next(connection_serial)
        born = time.monotonic()
        connection._ledger_profile_finalizer = weakref.finalize(
            connection, ledger_profile_finalize, serial, born)
        connection._ledger_profile_finalizer.atexit = False
        ledger_profile_event({"event": "created", "connection": serial,
                              "at_monotonic": born, "thread": threading.get_ident(),
                              "database": str(positional[0] if positional else keyword.get("database"))})
        return connection

    def ledger_profile_cpu(frame, event, argument):
        now = time.thread_time()
        thread = threading.get_ident()
        previous = getattr(states, "previous", now)
        cpu_totals[thread][getattr(states, "phase", "other")] += now - previous
        states.previous = now
        states.phase = ledger_profile_frame_category(
            frame, source_directory,
            event == "c_call" and isinstance(
                getattr(argument, "__self__", None), (sqlite3.Connection, sqlite3.Cursor))
            and getattr(argument, "__name__", None) in LEDGER_PROFILE_SQL_METHODS)

    def ledger_profile_worker_finish():
        sys.setprofile(None)
        threading.setprofile(None)
        snapshot = {thread: dict(phases) for thread, phases in list(cpu_totals.items())}
        ledger_profile_event({"event": "cpu", "threads": snapshot,
                              "scope": "post-install thread CPU; profiler overhead included; "
                                       "sitecustomize unobserved; cursor iteration instrumented"})
        ledger_profile_event({"event": "observation_end", "at_monotonic": time.monotonic()})

    sqlite3.connect = ledger_profile_connect
    sqlite3.dbapi2.connect = ledger_profile_connect
    threading.setprofile(ledger_profile_cpu)
    sys.setprofile(ledger_profile_cpu)
    atexit.register(ledger_profile_worker_finish)
    def ledger_profile_terminate(_signum, _frame):
        raise SystemExit()
    signal.signal(signal.SIGTERM, ledger_profile_terminate)
    sys.argv = [os.environ["LEDGER_PROFILE_DAEMON"], *sys.argv[2:]]
    runpy.run_path(sys.argv[0], run_name="__main__")


def ledger_profile_summarize(output):
    source = output / "telemetry.jsonl"
    if not source.exists():
        return
    events = [json.loads(line) for line in source.read_text().splitlines()]
    created = {item["connection"]: item for item in events if item["event"] == "created"}
    closed = {item["connection"]: item for item in events if item["event"] == "finalized"}
    ends = [item["at_monotonic"] for item in events if item["event"] == "observation_end"]
    work_units = int((output / "consumer_response.txt").exists())
    summary = {"connections_created": len(created), "consumer_work_units": work_units,
               "connections_per_work_unit": len(created) / work_units if work_units else None,
               "closed_lifetimes_seconds": {key: item["lifetime_seconds"] for key, item in closed.items()},
               "right_censored_lifetimes_seconds": {
                   key: ends[-1] - item["at_monotonic"] for key, item in created.items()
                   if key not in closed} if ends else {},
               "cpu_observations": [item for item in events if item["event"] == "cpu"],
               "central_cost_split": "UNMEASURED unless complete CPU observation_end exists",
               "coverage": "Only connections after worker instrumentation; startup/sitecustomize excluded"}
    cpu_events = summary["cpu_observations"]
    if cpu_events and ends:
        totals = defaultdict(float)
        for item in cpu_events:
            for thread in item["threads"].values():
                for phase, seconds in thread.items():
                    totals[phase] += seconds
        cpu = sum(seconds for phase, seconds in totals.items() if phase != "query_native_seconds")
        setup = totals["ledger_setup"]
        query = totals["query_native_seconds"]
        if cpu > 0 and setup + query <= cpu:
            summary["central_cost_split"] = {
                "setup_thread_cpu_fraction": setup / cpu,
                "query_thread_cpu_fraction": query / cpu,
                "other_thread_cpu_fraction": (cpu - setup - query) / cpu,
                "observed_thread_cpu_seconds": cpu,
                "limitation": "Instrumented post-install thread CPU only, includes profiler overhead; "
                              "not whole-process startup CPU; compare required py-spy capture"}
    (output / "accounting.json").write_text(json.dumps(summary, indent=2))


def ledger_profile_command(command, destination):
    result = subprocess.run(command, text=True, capture_output=True, check=False)
    destination.write_text(result.stdout + result.stderr)
    if result.returncode:
        raise RuntimeError(result.stdout + result.stderr)
    return result.stdout


def ledger_profile_capture(args, pid):
    dump = [args.py_spy, "dump", "--pid", str(pid)]
    record = [args.py_spy, "record", "--pid", str(pid), "--duration",
              str(args.duration), "--threads", "--full-filenames",
              "--output", str(args.output / "flame.svg")]
    (args.output / "host_commands.txt").write_text(
        shlex.join([args.sudo, *dump]) + "\n" + shlex.join([args.sudo, *record]) + "\n")
    # ps -M supplies thread state, not native backtraces on macOS. Do not claim
    # sqlite3_step from an R state or silently substitute another profiler.
    shape = ledger_profile_command(
        [args.ps, "-M", "-p", str(pid), "-o", "pid,tid,stat"], args.output / "threads.txt")
    # ps supplies R states; native sample supplies the sqlite3_step stack. sample
    # is used only as the shape gate, never instead of the required py-spy capture.
    native = ledger_profile_command(
        [args.sample, str(pid), str(args.shape_duration)], args.output / "native_shape.txt")
    if not ledger_profile_running_step_threads(shape, native):
        raise RuntimeError("Burst shape NOT CONFIRMED: require same running TID with sqlite3_step; "
                           "no cost split captured. Host commands are in host_commands.txt.")
    ledger_profile_command([args.sudo, "-n", *dump], args.output / "dump.txt")
    ledger_profile_command([args.sudo, "-n", *record], args.output / "record.txt")
    ledger_profile_command([args.sudo, "-n", args.py_spy, "record", "--pid", str(pid),
                           "--duration", str(args.duration), "--format", "speedscope",
                           "--threads", "--full-filenames",
                           "--output", str(args.output / "stacks.speedscope.json")],
                          args.output / "speedscope_record.txt")


def ledger_profile_rpc_payload(response):
    """Read the entire JSON or SSE response and select its JSON-RPC reply."""
    payload = response.read().decode()
    if response.headers.get_content_type() != "text/event-stream":
        return json.loads(payload)
    replies = []
    for block in payload.replace("\r\n", "\n").split("\n\n"):
        data = "\n".join(line.removeprefix("data:").lstrip(" ")
                         for line in block.splitlines() if line.startswith("data:"))
        if data:
            message = json.loads(data)
            if "id" in message and ("result" in message or "error" in message):
                replies.append(message)
    if len(replies) != 1:
        raise RuntimeError("Expected exactly one JSON-RPC reply in complete SSE response")
    return replies[0]


def ledger_profile_main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--daemon", required=True)
    parser.add_argument("--python", required=True, help="Gated interpreter for instrumented daemon")
    parser.add_argument("--source-ledger", required=True, type=Path,
                        help="Opened with mode=ro; SQLite backup API writes only isolated destination")
    parser.add_argument("--py-spy", required=True)
    parser.add_argument("--port", required=True, type=int)
    parser.add_argument("--host", required=True)
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--axis", required=True)
    parser.add_argument("--duration", required=True, type=int)
    parser.add_argument("--startup-timeout", required=True, type=float)
    parser.add_argument("--http-timeout", required=True, type=float)
    parser.add_argument("--poll-interval", required=True, type=float)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--sandbox-exec", required=True)
    parser.add_argument("--ps", required=True)
    parser.add_argument("--sudo", required=True)
    parser.add_argument("--sample", required=True)
    parser.add_argument("--shape-duration", required=True, type=int)
    parser.add_argument("--pid", type=int, help="Attach to an already isolated daemon; caller owns its lifecycle")
    args = parser.parse_args()
    if args.duration < MINIMUM_PROFILE_SECONDS:
        parser.error("duration must be at least 60 seconds")
    args.output = args.output.resolve()
    args.output.mkdir(parents=True, exist_ok=False)
    if args.pid:
        ledger_profile_capture(args, args.pid)
        return
    profile = args.output / "daemon.sb"
    # Grant loopback only to the daemon's own configured port — never every localhost port,
    # so a profiled daemon cannot reach an unrelated local service (e.g. another DB) on a
    # different port. Built from the --port input; no wildcard, no hardcoded port.
    profile.write_text(SANDBOX_TEMPLATE.format(port=args.port))
    env = os.environ.copy()
    env["SPENDGUARD_HOME"] = str(args.output / "spendguard")
    env["HONESTREVIEW_STATE"] = str(args.output / "honestreview")
    source_directory = args.root.resolve() / SOURCE_DIRECTORY_NAME
    if not (source_directory / "spendguard").is_dir():
        raise RuntimeError("--root must identify the spendguard worktree being diagnosed")
    env["PYTHONPATH"] = os.pathsep.join(filter(None, [str(source_directory), env.get("PYTHONPATH")]))
    env["LEDGER_PROFILE_SOURCE"] = str(source_directory)
    isolated_home = Path(env["SPENDGUARD_HOME"])
    isolated_home.mkdir()
    # Never copy a live db+wal. Online backup provides a transactionally consistent
    # snapshot while the source connection is strictly read-only.
    source_uri = args.source_ledger.resolve().as_uri() + "?mode=ro"
    with sqlite3.connect(source_uri, uri=True) as source:
        with sqlite3.connect(isolated_home / args.source_ledger.name) as target:
            source.backup(target)
    env["LEDGER_PROFILE_TELEMETRY"] = str(args.output / "telemetry.jsonl")
    env["LEDGER_PROFILE_DAEMON"] = args.daemon
    command = [args.sandbox_exec, "-D", f"OUTPUT={args.output}", "-f", str(profile),
               args.python, str(Path(__file__).resolve()), "--worker", "--http",
               "--host", args.host, "--port", str(args.port)]
    with (args.output / "daemon.log").open("w") as log:
        daemon = subprocess.Popen(command, env=env, stdout=log, stderr=subprocess.STDOUT)
        (args.output / "pid.txt").write_text(str(daemon.pid))
        try:
            # Fail at the actual OS sandbox / bind denial, preserving its exact text.
            deadline = time.monotonic() + args.startup_timeout
            url = f"http://{args.host}:{args.port}/mcp"
            body = {"jsonrpc": "2.0", "id": REVIEW_REQUEST_ID, "method": "tools/call",
                    "params": {"name": "review", "arguments": {
                        "axis": args.axis, "root": str(args.root.resolve()), "run": False}}}
            headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
            initialize = {"jsonrpc": "2.0", "id": "initialize", "method": "initialize", "params": {
                "protocolVersion": MCP_PROTOCOL_VERSION, "capabilities": {}, "clientInfo": MCP_PROBE_CLIENT}}
            request = urllib.request.Request(url, data=json.dumps(initialize).encode(), headers=headers)
            while True:
                if daemon.poll() is not None:
                    raise RuntimeError((args.output / "daemon.log").read_text())
                try:
                    with urllib.request.urlopen(request, timeout=args.http_timeout) as response:
                        initialized = ledger_profile_rpc_payload(response)
                        session = response.headers.get("Mcp-Session-Id")
                    if "error" in initialized:
                        raise RuntimeError(json.dumps(initialized))
                    if session:
                        headers["Mcp-Session-Id"] = session
                    headers["MCP-Protocol-Version"] = initialized["result"]["protocolVersion"]
                    break
                except urllib.error.URLError:
                    if time.monotonic() >= deadline:
                        raise RuntimeError("Daemon startup/request deadline exceeded; see daemon.log") from None
                    time.sleep(args.poll_interval)
            notification = {"jsonrpc": "2.0", "method": "notifications/initialized"}
            request = urllib.request.Request(url, data=json.dumps(notification).encode(), headers=headers)
            with urllib.request.urlopen(request, timeout=args.http_timeout) as response:
                response.read()
            request = urllib.request.Request(url, data=json.dumps(body).encode(), headers=headers)
            with urllib.request.urlopen(request, timeout=args.http_timeout) as response:
                result = ledger_profile_rpc_payload(response)
            if "error" in result or result.get("result", {}).get("isError"):
                raise RuntimeError("Consumer review failed: " + json.dumps(result))
            (args.output / "consumer_response.txt").write_text(json.dumps(result))
            ledger_profile_capture(args, daemon.pid)
        finally:
            if daemon.poll() is None:
                daemon.terminate()
                daemon.wait()
            ledger_profile_summarize(args.output)


if __name__ == "__main__":
    try:
        if len(sys.argv) > 1 and sys.argv[1] == "--worker":
            ledger_profile_worker()
        else:
            ledger_profile_main()
    except (OSError, RuntimeError) as error:
        print(str(error), file=sys.stderr)
        sys.exit(1)

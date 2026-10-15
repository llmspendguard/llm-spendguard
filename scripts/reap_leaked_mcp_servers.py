#!/usr/bin/env python3
"""Trim current-user stdio MCP strays; dry-run unless --apply. No LLM calls.

Identity comes only from exact argv elements matching Codex's configured command.
macOS kernel argv avoids the ambiguous, flattened command strings printed by ps.
"""
import argparse
import ctypes
import errno
import os
from pathlib import Path
import platform
import signal
import subprocess
import sys

try:
    import tomllib
except ImportError:
    tomllib = None

DEFAULT_SERVERS = "honestreview"
DEFAULT_MAX_KEEP = 3
DEFAULT_MIN_AGE_S = 90
DEFAULT_CODEX_HOME = "~/.codex"


def gap(name, detail):
    print("GAP {}: {}".format(name, detail), file=sys.stderr)


def settings():
    servers = os.environ.get("SPENDGUARD_MCP_REAP_SERVERS", DEFAULT_SERVERS)
    names = list(dict.fromkeys(s.strip() for s in servers.split(",") if s.strip()))
    keep = int(os.environ.get("SPENDGUARD_MCP_REAP_MAX_KEEP", DEFAULT_MAX_KEEP))
    age = int(os.environ.get("SPENDGUARD_MCP_REAP_MIN_AGE_S", DEFAULT_MIN_AGE_S))
    if not names or keep < 0 or age < 0:
        raise ValueError("servers must be nonempty; MAX_KEEP and MIN_AGE_S must be nonnegative")
    return names, keep, age


def configured_commands(names):
    path = Path(os.environ.get("CODEX_HOME", DEFAULT_CODEX_HOME)).expanduser() / "config.toml"
    if tomllib is None:
        raise ValueError("{}: tomllib unavailable; use Python 3.11+".format(path))
    with path.open("rb") as source:
        config = tomllib.load(source)
    commands = {}
    for name in names:
        command = config.get("mcp_servers", {}).get(name, {}).get("command")
        if not isinstance(command, str) or not command:
            raise ValueError("{}: mcp_servers.{}.command missing or invalid".format(path, name))
        commands[name] = command
    return commands


def launchd_pids():
    output = subprocess.check_output(["launchctl", "list"], text=True)
    lines = output.splitlines()
    if not lines or lines[0].split() != ["PID", "Status", "Label"]:
        raise ValueError("unexpected launchctl list header")
    pids = set()
    for line in lines[1:]:
        pid, status, label = line.split(None, 2)
        if pid != "-":
            pids.add(int(pid))
    return pids


def process_argv(pid):
    # KERN_PROCARGS2 returns argc, executable path, padding, then NUL-separated argv.
    libc = ctypes.CDLL(None, use_errno=True)
    mib = (ctypes.c_int * 3)(1, 49, pid)  # CTL_KERN, KERN_PROCARGS2
    size = ctypes.c_size_t()
    if libc.sysctl(mib, 3, None, ctypes.byref(size), None, 0) != 0:
        code = ctypes.get_errno()
        raise OSError(code, os.strerror(code))
    buffer = ctypes.create_string_buffer(size.value)
    if libc.sysctl(mib, 3, buffer, ctypes.byref(size), None, 0) != 0:
        code = ctypes.get_errno()
        raise OSError(code, os.strerror(code))
    data = buffer.raw[:size.value]
    argc = int.from_bytes(data[:ctypes.sizeof(ctypes.c_int)], sys.byteorder, signed=True)
    start = data.index(b"\0", ctypes.sizeof(ctypes.c_int)) + 1
    while start < len(data) and data[start] == 0:
        start += 1
    return [os.fsdecode(arg) for arg in data[start:].split(b"\0")[:argc]]


def elapsed_seconds(value):
    days, sep, clock = value.rpartition("-")
    parts = [int(p) for p in clock.split(":")]
    seconds = 0
    for part in parts:
        seconds = seconds * 60 + part
    return seconds + (int(days) * 86400 if sep else 0)


def processes():
    # Only our own processes: avoids inaccessible argv and other users' sessions.
    output = subprocess.check_output(
        ["ps", "-ww", "-U", str(os.getuid()), "-o", "pid=,ppid=,etime="], text=True
    )
    for row in output.splitlines():
        pid, ppid, elapsed = row.split()
        yield int(pid), int(ppid), elapsed_seconds(elapsed)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="send SIGTERM (default: dry-run)")
    args = parser.parse_args()
    print("PROVENANCE python={} tomllib={} mode={}".format(
        platform.python_version(), "available" if tomllib else "unavailable",
        "APPLY" if args.apply else "DRY-RUN"), flush=True)
    try:
        names, keep, min_age = settings()
    except ValueError as exc:
        gap("SETTINGS", exc)
        return 1
    try:
        commands = configured_commands(names)
    except (OSError, ValueError, TypeError, AttributeError) as exc:
        gap("CODEX_CONFIG", exc)
        return 1
    if sys.platform != "darwin":
        gap("HOST_PLATFORM", "this launchd reaper requires macOS")
        return 1
    try:
        protected = launchd_pids()
        rows = list(processes())
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        gap("PROCESS_INVENTORY", exc)
        return 1
    print("POLICY servers={} MAX_KEEP={} MIN_AGE_S={} launchd_protected={}".format(
        ",".join(names), keep, min_age, len(protected)))
    instances = {name: [] for name in names}
    failed = False
    unreadable = 0
    protected_skipped = 0
    for pid, ppid, age in rows:
        if pid in protected or pid == os.getpid():
            protected_skipped += 1
            continue
        try:
            argv = process_argv(pid)
        except OSError as exc:
            # ESRCH = exited mid-scan; EINVAL/EPERM/etc = argv not introspectable.
            # Either way cannot confirm identity, so never a target: skip without failing.
            if exc.errno != errno.ESRCH:
                unreadable += 1
            continue
        for name, command in commands.items():
            if command in argv:
                instances[name].append((pid, ppid, age))
    if unreadable:
        print("UNREADABLE_ARGV count={} (argv not introspectable; not identity-matchable, skipped)".format(unreadable))
    print("PROTECTED_SKIPPED count={} (launchd-managed or self; never candidates)".format(protected_skipped))
    targets = {}
    for name, matches in instances.items():
        # Smallest elapsed time = newest. PID breaks second-resolution ties.
        matches.sort(key=lambda row: (row[2], -row[0]))
        for index, (pid, ppid, age) in enumerate(matches):
            reasons = []
            if ppid == 1:
                reasons.append("orphan")
            if index >= keep:
                reasons.append("beyond_MAX_KEEP")
            if not reasons:
                continue
            if age < min_age:
                print("HOLDING pid={} under MIN_AGE_S server={} age_s={} reasons={}".format(
                    pid, name, age, ",".join(reasons)))
                continue
            targets.setdefault(pid, []).append((name, ",".join(reasons)))
    for pid, reasons in targets.items():
        detail = "pid={} servers={} reasons={}".format(
            pid, ",".join(name for name, _ in reasons), ";".join(r for _, r in reasons))
        if not args.apply:
            print("DRY-RUN would SIGTERM " + detail)
            continue
        try:
            # Recheck membership and identity immediately before signaling.
            if pid in launchd_pids():
                print("PROTECTED launchd " + detail)
                continue
            argv = process_argv(pid)
            if not any(commands[name] in argv for name, _ in reasons):
                print("SKIPPED identity-changed " + detail)
                continue
            os.kill(pid, signal.SIGTERM)
            print("SIGTERM sent " + detail)
        except ProcessLookupError:
            print("ALREADY-GONE " + detail)
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
            gap("SIGTERM_OUTCOME", "{} error={}".format(detail, exc))
            failed = True
    print("DONE candidates={} mode={}".format(len(targets), "APPLY" if args.apply else "DRY-RUN"))
    return int(failed)


if __name__ == "__main__":
    sys.exit(main())

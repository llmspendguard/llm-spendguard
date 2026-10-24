#!/usr/bin/env python3
"""Run isolated offline files in a bounded pool, streaming completed verdicts.

Use --sequential for the original chunk loop, --chunk i/N for a single CI subset,
or --write-manifest --shard-count N to promote complete local timing evidence.
Full runs atomically record durations without modifying the committed manifest.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
import glob
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
TESTS = os.path.join(REPO, "tests")
sys.path.insert(0, TESTS)
from _gate_shard_plan import (  # noqa: E402
    GATE_SHARD_MANIFEST_FILENAME, GATE_SHARD_MINIMUM_COUNT,
    GATE_SHARD_SCHEMA_VERSION, GateShardEvidenceError, validate_gate_shard_manifest,
)
from spendguard.config import CLI_TEST_OFFLINE_ENVIRONMENT_KEY, CLI_TEST_OFFLINE_ENABLED_VALUE  # noqa: E402

DEFAULT_MAX_PARALLEL = max(1, (os.cpu_count() or 2) - 2)
LOCAL_DURATION_CACHE = os.path.join(REPO, ".test_durations.local.json")
DEFAULT_CHUNK_COUNT = 6
SLOWEST_FILE_COUNT = 10
CHUNKED_NODEID_TEMPLATE = "tests/test_runner.py::test_script[{}]"


def _chunked_manifest_payload(durations, shard_count):
    return validate_gate_shard_manifest({"schema_version": GATE_SHARD_SCHEMA_VERSION,
                                         "shard_count": shard_count, "durations": durations})


def _chunked_atomic_json(path, payload):
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", dir=os.path.dirname(path), delete=False) as stream:
            temporary = stream.name
            json.dump(payload, stream, indent=2, sort_keys=True)
            stream.write("\n")
        os.replace(temporary, path)
    finally:
        if temporary and os.path.exists(temporary):
            os.unlink(temporary)


def _chunked_promote_cache(files, shard_count):
    with open(LOCAL_DURATION_CACHE) as stream:
        cache = json.load(stream)
    if type(cache.get("coverage")) is not bool or cache["coverage"] != (os.environ.get("SPENDGUARD_COVERAGE") == "1"):
        raise GateShardEvidenceError("cache coverage regime differs from requested regime")
    if cache.get("schema_version") != GATE_SHARD_SCHEMA_VERSION:
        raise GateShardEvidenceError("unsupported cache schema")
    manifest = _chunked_manifest_payload(cache.get("durations"), shard_count)
    if set(manifest["durations"]) != {CHUNKED_NODEID_TEMPLATE.format(os.path.basename(path)) for path in files}:
        raise GateShardEvidenceError("cache inventory differs from complete suite")
    _chunked_atomic_json(os.path.join(REPO, GATE_SHARD_MANIFEST_FILENAME), manifest)


def _chunked_timed_file(path):
    started = time.perf_counter()
    result = _run_one(path)
    return result, time.perf_counter() - started
FILE_BUDGET_S = 600                      # per-file timeout — MIRROR tests/test_runner.py's `timeout=600` (they must stay
#                                          in step). 120 here silently diverged and false-failed `spendguard deploy` when a
#                                          slow-but-correct test (e.g. test_resources_gpu) ran on a loaded machine; 600
#                                          still catches real sleeps / runaway loops.
_STRIP_KEYS = ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "VAST_API_KEY", "GEMINI_API_KEY",
               "RUNPOD_API_KEY", "MODAL_TOKEN_ID", "MODAL_TOKEN_SECRET", "LAMBDA_API_KEY")


def _test_files():
    return sorted(f for f in glob.glob(os.path.join(TESTS, "test_*.py"))
                  if os.path.basename(f) != "test_runner.py")


GATE_ISOLATED_HOME_ENV_NAMES = ("HOME", "SPENDGUARD_HOME")
GATE_ISOLATED_TMPDIR_ENV_NAME = "TMPDIR"
GATE_TIKTOKEN_CACHE_ENV_NAME = "TIKTOKEN_CACHE_DIR"
GATE_TIKTOKEN_CACHE_DIRECTORY_NAME = "data-gym-cache"
GATE_AMBIENT_TMPDIR = tempfile.gettempdir()
GATE_AMBIENT_TIKTOKEN_CACHE_DIRECTORY = os.environ.get(GATE_TIKTOKEN_CACHE_ENV_NAME)
GATE_TIKTOKEN_CACHE_DIRECTORY = os.path.join(GATE_AMBIENT_TMPDIR, GATE_TIKTOKEN_CACHE_DIRECTORY_NAME)
GATE_RUNNER_TMPDIR_BASE_ENV = "SPENDGUARD_GATE_TMPDIR_BASE"
GATE_SHORT_TMPDIR_BASE = os.path.realpath(os.environ.get(GATE_RUNNER_TMPDIR_BASE_ENV, GATE_AMBIENT_TMPDIR))
DARWIN_AF_UNIX_SUN_PATH_CAPACITY_BYTES = 104
AF_UNIX_PATH_TERMINATOR_BYTES = 1
GATE_BROKER_NESTED_TMPDIR_PREFIX = "sgc-"
GATE_BROKER_NESTED_TMPDIR_RANDOM_SUFFIX = "XXXXXXXX"
GATE_BROKER_TEST_SOCKET_FILENAME = "gate.sock"
GATE_BROKER_DEFAULT_SOCKET_FILENAME = "codex-daemon.sock"
GATE_TEST_ISOLATED_ENV = "SPENDGUARD_TEST_ISOLATED"
GATE_TEST_ENABLED = "1"


class GateTestEnvironmentError(RuntimeError):
    """The runner refused an environment that could reach real user state."""


def validate_gate_child_environment(env, scratch_home, scratch_tmpdir):
    """Require the canonical per-file roots immediately before child startup."""
    expected_home = os.path.realpath(scratch_home)
    for root_name in GATE_ISOLATED_HOME_ENV_NAMES:
        root_value = env.get(root_name)
        if not root_value or os.path.realpath(root_value) != expected_home:
            raise GateTestEnvironmentError(f"test isolation violated: {root_name} escaped scratch home")
    expected_tmpdir = os.path.realpath(scratch_tmpdir)
    tmpdir_value = env.get(GATE_ISOLATED_TMPDIR_ENV_NAME)
    if not tmpdir_value or os.path.realpath(tmpdir_value) != expected_tmpdir:
        raise GateTestEnvironmentError("test isolation violated: TMPDIR escaped per-file temp directory")
    tiktoken_cache_value = env.get(GATE_TIKTOKEN_CACHE_ENV_NAME)
    if not tiktoken_cache_value or not os.path.isdir(tiktoken_cache_value):
        raise GateTestEnvironmentError("test isolation violated: stable tiktoken cache missing")
    if env.get(GATE_TEST_ISOLATED_ENV) != GATE_TEST_ENABLED:
        raise GateTestEnvironmentError("test isolation violated: isolated test signal missing")
    if env.get(CLI_TEST_OFFLINE_ENVIRONMENT_KEY) != CLI_TEST_OFFLINE_ENABLED_VALUE:
        raise GateTestEnvironmentError("test isolation violated: CLI offline signal missing")


def _child_env(scratch_home, scratch_tmpdir):
    env = dict(os.environ)
    for root_name in GATE_ISOLATED_HOME_ENV_NAMES:
        env[root_name] = os.path.realpath(scratch_home)
    env[GATE_ISOLATED_TMPDIR_ENV_NAME] = os.path.realpath(scratch_tmpdir)
    env[GATE_RUNNER_TMPDIR_BASE_ENV] = GATE_SHORT_TMPDIR_BASE
    env[GATE_TIKTOKEN_CACHE_ENV_NAME] = (
        GATE_AMBIENT_TIKTOKEN_CACHE_DIRECTORY or os.path.realpath(GATE_TIKTOKEN_CACHE_DIRECTORY)
    )
    env[GATE_TEST_ISOLATED_ENV] = GATE_TEST_ENABLED
    env[CLI_TEST_OFFLINE_ENVIRONMENT_KEY] = CLI_TEST_OFFLINE_ENABLED_VALUE
    # Mirror test_runner.py so offline runners stay in step: deterministic short recovery schedules; chaos sets its own timing explicitly.
    env["SPENDGUARD_LANE_BACKOFF_S"] = "0"
    env["SPENDGUARD_SDK_MAX_RETRIES"] = "0"
    env["SPENDGUARD_LANE_QUEUE_DEADLINE_S"] = "0"
    # OFFLINE, ENFORCED — identical to test_runner.py: a dead proxy makes any external call fail in ms (loud,
    # not slow); localhost servers a test spins up itself stay reachable via no_proxy. Real keys are stripped.
    env["http_proxy"] = env["https_proxy"] = env["HTTP_PROXY"] = env["HTTPS_PROXY"] = "http://127.0.0.1:9"
    env["no_proxy"] = env["NO_PROXY"] = "localhost,127.0.0.1"
    for k in _STRIP_KEYS:
        env.pop(k, None)
    validate_gate_child_environment(env, scratch_home, scratch_tmpdir)
    return env


# These compact leaf names preserve enough sockaddr_un capacity when this runner
# is itself executed beneath the pytest runner's isolated TMPDIR. Ownership is
# established by the exact paths returned from mkdtemp, never by this prefix.
SCRATCH_HOME_PREFIX = "h-"
GATE_SCRATCH_TMPDIR_PREFIX = "t-"


def validate_gate_broker_socket_path_capacity(scratch_home, scratch_tmpdir):
    """Keep every broker socket exercised by the gate within Darwin's sockaddr_un.sun_path."""
    if not hasattr(socket, "AF_UNIX") or sys.platform != "darwin":
        return
    nested_test_socket = os.path.join(
        scratch_tmpdir,
        GATE_BROKER_NESTED_TMPDIR_PREFIX + GATE_BROKER_NESTED_TMPDIR_RANDOM_SUFFIX,
        GATE_BROKER_TEST_SOCKET_FILENAME,
    )
    default_broker_socket = os.path.join(scratch_home, GATE_BROKER_DEFAULT_SOCKET_FILENAME)
    longest_socket_path = max((nested_test_socket, default_broker_socket), key=lambda path: len(os.fsencode(path)))
    required_bytes = len(os.fsencode(longest_socket_path)) + AF_UNIX_PATH_TERMINATOR_BYTES
    if required_bytes > DARWIN_AF_UNIX_SUN_PATH_CAPACITY_BYTES:
        raise GateTestEnvironmentError(
            f"test isolation violated: broker socket needs {required_bytes} bytes, "
            f"Darwin sun_path holds {DARWIN_AF_UNIX_SUN_PATH_CAPACITY_BYTES}"
        )


def _run_one(path):
    # Only files under tests/ are ever spawned (symlink-resolved), so a stray path can never escape the repo.
    real = os.path.realpath(path)
    tests_root = os.path.realpath(TESTS) + os.sep
    if not real.startswith(tests_root):
        return 2, "", f"refused: {path} is not under {TESTS}"
    scratch_home = tempfile.mkdtemp(prefix=SCRATCH_HOME_PREFIX, dir=GATE_SHORT_TMPDIR_BASE)
    scratch_tmpdir = None
    try:
        scratch_tmpdir = tempfile.mkdtemp(prefix=GATE_SCRATCH_TMPDIR_PREFIX, dir=GATE_SHORT_TMPDIR_BASE)
        validate_gate_broker_socket_path_capacity(scratch_home, scratch_tmpdir)
        env = _child_env(scratch_home, scratch_tmpdir)
        p = subprocess.run([sys.executable, real], env=env, timeout=FILE_BUDGET_S,
                           capture_output=True, text=True)
        return p.returncode, p.stdout, p.stderr
    except subprocess.TimeoutExpired:
        return 124, "", f"TIMEOUT after {FILE_BUDGET_S}s"
    finally:
        # The scratch home is per-file and disposable. Without this, every suite run left ~90 dirs behind
        # (some 0.5GB each — the codex/claudecode state tests write a ~110MB json plus its rotated backups);
        # measured 2026-09-14: 7,794 dirs / 29.9GB in $TMPDIR with the disk at 100%.
        shutil.rmtree(scratch_home, ignore_errors=True)
        if scratch_tmpdir is not None:
            shutil.rmtree(scratch_tmpdir, ignore_errors=True)


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--chunks", type=int, default=DEFAULT_CHUNK_COUNT, help="how many chunks to split the suite into")
    ap.add_argument("--chunk", default=None, help="run ONLY chunk i/N (e.g. 2/6), for parallel shells")
    ap.add_argument("--max-parallel", type=int, default=DEFAULT_MAX_PARALLEL)
    ap.add_argument("--sequential", action="store_true", help="use the sequential chunk loop")
    ap.add_argument("--write-manifest", action="store_true", help="promote the complete local cache without running tests")
    ap.add_argument("--shard-count", type=int)
    a = ap.parse_args(argv)
    if a.max_parallel < GATE_SHARD_MINIMUM_COUNT:
        ap.error("--max-parallel must be positive")
    if a.write_manifest and (a.chunk or a.shard_count is None):
        ap.error("--write-manifest requires --shard-count and cannot use --chunk")

    files = _test_files()
    if not files:
        print("no test files found", file=sys.stderr)
        return 2
    if a.write_manifest:
        try:
            _chunked_promote_cache(files, a.shard_count)
        except (OSError, ValueError, GateShardEvidenceError) as error:
            print(f"manifest refused: {error}", file=sys.stderr)
            return 1
        return 0

    # LINT GATE FIRST — mirror CI (`ruff check src tests`), so "green locally" means "green in the release build".
    # A suite that passes but fails lint still fails the release; this catches it in seconds instead of at the tag.
    try:
        lint = subprocess.run([sys.executable, "-m", "ruff", "check", "src", "tests"], cwd=REPO,
                              capture_output=True, text=True)
        if lint.returncode == 0:
            print("[lint] ok: ruff check src tests", flush=True)
        else:
            print("[lint] FAIL: ruff check src tests — fix ruff before the suite (mirrors the release build).", flush=True)
            print("\n".join((lint.stdout or "").splitlines()[-20:]), flush=True)
            return 1                                     # lint is the first gate; stop here (it runs in seconds)
    except FileNotFoundError:
        print("[lint] SKIPPED: ruff not installed (pip install ruff) — CI still enforces it", flush=True)

    n = max(1, a.chunks)
    chunks = [files[i::n] for i in range(n)]     # round-robin → each chunk a representative mix, similar wall-time
    only = None
    if a.chunk:
        i, _, tot = a.chunk.partition("/")
        only, n2 = int(i), int(tot or n)
        if n2 < GATE_SHARD_MINIMUM_COUNT or not GATE_SHARD_MINIMUM_COUNT <= only <= n2:
            ap.error("--chunk must select a valid i/N")
        if n2 != n:
            chunks = [files[j::n2] for j in range(n2)]

    total_fail, total_run = [], 0
    durations = {}
    suite_started = time.perf_counter()
    if not a.sequential and only is None:
        with ThreadPoolExecutor(max_workers=a.max_parallel) as pool:
            pending = {pool.submit(_chunked_timed_file, path): path for path in files}
            for future in as_completed(pending):
                path = pending[future]
                (rc, out, err), elapsed = future.result()
                name = os.path.basename(path)
                durations[CHUNKED_NODEID_TEMPLATE.format(name)] = elapsed
                total_run += 1
                print(f"[file] {'FAIL' if rc else 'PASS'}: {name} ({elapsed:.2f}s)", flush=True)
                if rc:
                    total_fail.append((name, rc, out, err))
                    print(f"    exit {rc}\n{out}\n    stderr: {err}", flush=True)
    for idx, chunk in enumerate(chunks if a.sequential or only is not None else [], 1):
        if only is not None and idx != only:
            continue
        t0 = time.time()
        fails = []
        for path in chunk:
            (rc, out, err), elapsed = _chunked_timed_file(path)
            durations[CHUNKED_NODEID_TEMPLATE.format(os.path.basename(path))] = elapsed
            total_run += 1
            if rc != 0:
                fails.append((os.path.basename(path), rc, out, err))
        tag = "FAIL" if fails else "ok"
        print(f"[chunk {idx}/{len(chunks)}] {tag}: {len(chunk) - len(fails)}/{len(chunk)} passed "
              f"({time.time() - t0:.0f}s)", flush=True)
        for name, rc, out, err in fails:
            tail = "\n".join((out or "").splitlines()[-6:])
            print(f"    ✗ {name} (exit {rc})\n{tail}\n    stderr: {(err or '').strip()[:200]}", flush=True)
        total_fail += fails

    wall_s = time.perf_counter() - suite_started
    if only is None:
        expected = {CHUNKED_NODEID_TEMPLATE.format(os.path.basename(path)) for path in _test_files()}
        if total_run != len(files) or set(durations) != expected:
            raise GateShardEvidenceError("full suite timing inventory is incomplete")
        _chunked_manifest_payload(durations, GATE_SHARD_MINIMUM_COUNT)
        _chunked_atomic_json(LOCAL_DURATION_CACHE, {
            "schema_version": GATE_SHARD_SCHEMA_VERSION,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "coverage": os.environ.get("SPENDGUARD_COVERAGE") == "1",
            "wall_s": wall_s, "max_parallel": GATE_SHARD_MINIMUM_COUNT if a.sequential else a.max_parallel,
            "durations": durations,
        })
        print(f"[timing] total wall {wall_s:.2f}s; CPU sum (file wall-times) {sum(durations.values()):.2f}s", flush=True)
        print(f"[timing] TOP {SLOWEST_FILE_COUNT} slowest files:", flush=True)
        for nodeid, elapsed in sorted(durations.items(), key=lambda item: (-item[1], item[0]))[:SLOWEST_FILE_COUNT]:
            print(f"    {nodeid}: {elapsed:.2f}s", flush=True)

    print(f"\n{'FAIL' if total_fail else 'OK'}: {total_run - len(total_fail)}/{total_run} files passed"
          f"{' — ' + ', '.join(f[0] for f in total_fail) if total_fail else ''}")
    return 1 if total_fail else 0


if __name__ == "__main__":
    sys.exit(main())

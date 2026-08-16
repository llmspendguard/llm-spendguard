#!/usr/bin/env python3
"""Run the offline test suite in CHUNKS, reporting each chunk's pass/fail as it finishes — never one buffered
all-or-nothing run whose exit code can be masked (and once was, pushing a red state).

Each test_*.py self-isolates (execv into a fresh SPENDGUARD_HOME); this replicates test_runner.py's environment
exactly — isolated home per file, a DEAD proxy so any accidental network fails in ms, real provider keys
stripped — then runs the files in N chunks. A chunk prints PASS/FAIL the moment it completes, so progress is
visible; the process exits non-zero if ANY file failed. Grouping is by sorted filename so chunks are stable.

    python scripts/test/chunked_suite.py            # 6 chunks (default)
    python scripts/test/chunked_suite.py --chunks 4
    python scripts/test/chunked_suite.py --chunk 2/6 # run only the 2nd of 6 chunks (for parallel shells)
"""
import argparse
import glob
import os
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
TESTS = os.path.join(REPO, "tests")
FILE_BUDGET_S = 120                      # same backstop test_runner.py uses (catches sleeps / runaway loops)
_STRIP_KEYS = ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "VAST_API_KEY", "GEMINI_API_KEY",
               "RUNPOD_API_KEY", "MODAL_TOKEN_ID", "MODAL_TOKEN_SECRET", "LAMBDA_API_KEY")


def _test_files():
    return sorted(f for f in glob.glob(os.path.join(TESTS, "test_*.py"))
                  if os.path.basename(f) != "test_runner.py")


def _child_env():
    env = dict(os.environ)
    env["SPENDGUARD_TEST_ISOLATED"] = "1"
    # OFFLINE, ENFORCED — identical to test_runner.py: a dead proxy makes any external call fail in ms (loud,
    # not slow); localhost servers a test spins up itself stay reachable via no_proxy. Real keys are stripped.
    env["http_proxy"] = env["https_proxy"] = env["HTTP_PROXY"] = env["HTTPS_PROXY"] = "http://127.0.0.1:9"
    env["no_proxy"] = env["NO_PROXY"] = "localhost,127.0.0.1"
    for k in _STRIP_KEYS:
        env.pop(k, None)
    return env


def _run_one(path):
    env = _child_env()
    env["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="sg-chunk-")
    try:
        p = subprocess.run([sys.executable, path], env=env, timeout=FILE_BUDGET_S,
                           capture_output=True, text=True)
        return p.returncode, p.stdout, p.stderr
    except subprocess.TimeoutExpired:
        return 124, "", f"TIMEOUT after {FILE_BUDGET_S}s"


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--chunks", type=int, default=6, help="how many chunks to split the suite into")
    ap.add_argument("--chunk", default=None, help="run ONLY chunk i/N (e.g. 2/6), for parallel shells")
    a = ap.parse_args(argv)

    files = _test_files()
    if not files:
        print("no test files found", file=sys.stderr)
        return 2

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
        if n2 != n:
            chunks = [files[j::n2] for j in range(n2)]

    total_fail, total_run = [], 0
    for idx, chunk in enumerate(chunks, 1):
        if only is not None and idx != only:
            continue
        t0 = time.time()
        fails = []
        for path in chunk:
            rc, out, err = _run_one(path)
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

    print(f"\n{'FAIL' if total_fail else 'OK'}: {total_run - len(total_fail)}/{total_run} files passed"
          f"{' — ' + ', '.join(f[0] for f in total_fail) if total_fail else ''}")
    return 1 if total_fail else 0


if __name__ == "__main__":
    sys.exit(main())

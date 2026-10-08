#!/usr/bin/env python3
"""Warm Kimi lane (kimi_daemon) vs a FAKE `kimi acp` ACP server — the JSON-RPC/ACP protocol flow: initialize →
session/new → session/prompt, session/update (agent_message_chunk) demux by sessionId, concurrency, the warm-POOL
property (N prompts, ONE cold-start), a wedged turn that fails only itself, a hard refusal, the no-hang reply to a
server-initiated request, and dead-pipe wakeups. Offline, no real kimi, no model spend.
"""
import os
import sys
import tempfile
import pathlib
from concurrent.futures import ThreadPoolExecutor

os.environ.setdefault("SPENDGUARD_HOME", tempfile.mkdtemp(prefix="sg-kacp-"))
os.environ.setdefault("SPENDGUARD_TEST_ISOLATED", "1")
os.environ.setdefault("SPENDGUARD_NO_AUTOINSTALL", "1")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from spendguard import kimi_daemon        # noqa: E402

FAKE = pathlib.Path(__file__).with_name("fake_kimi_acp_server.py").resolve()


def new_daemon(boot_log=None):
    os.environ["SPENDGUARD_KIMI_BIN"] = str(FAKE)
    if boot_log is not None:
        os.environ["FAKE_KIMI_BOOT_LOG"] = str(boot_log)
    else:
        os.environ.pop("FAKE_KIMI_BOOT_LOG", None)
    return kimi_daemon._KimiAcpDaemon()


fails = []


def ck(name, cond):
    print(("  [OK] " if cond else "  [FAIL] ") + name)
    if not cond:
        fails.append(name)


def test_agent_message():
    daemon = new_daemon()
    try:
        r = daemon.run_warm("hello")
        ck("basic warm reply, reasoning chunk excluded", r == {"text": "reply:hello", "error": None})
    finally:
        daemon.shutdown()


def test_concurrent_turns_are_demultiplexed():
    daemon = new_daemon()
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            a = pool.submit(daemon.run_warm, "first")
            b = pool.submit(daemon.run_warm, "second")
            results = [a.result(timeout=10), b.result(timeout=10)]
        ck("concurrent turns demuxed to their own text",
           sorted(x["text"] for x in results) == ["reply:first", "reply:second"])
    finally:
        daemon.shutdown()


def test_n_prompts_one_cold_start():
    """The warm-POOL guarantee: N independent prompts are served by ONE server boot, not N cold starts."""
    boot_log = pathlib.Path(tempfile.mkdtemp(prefix="sg-kboot-")) / "boots.txt"
    daemon = new_daemon(boot_log=boot_log)
    try:
        texts = [daemon.run_warm(f"p{i}")["text"] for i in range(4)]
        boots = boot_log.read_text().strip().splitlines() if boot_log.exists() else []
        ck("all 4 prompts answered", texts == [f"reply:p{i}" for i in range(4)])
        ck("4 prompts served by exactly ONE cold-start (not 4)", len(boots) == 1)
    finally:
        daemon.shutdown()


def test_wedged_turn_fails_only_itself_others_survive():
    """A single wedged turn times out to its OWN error; concurrent healthy turns still complete and the server lives."""
    daemon = new_daemon()
    try:
        with ThreadPoolExecutor(max_workers=3) as pool:
            wedged = pool.submit(daemon.run_warm, "wedge", None, None, 1)   # (prompt, model, reasoning, timeout=1s)
            ok1 = pool.submit(daemon.run_warm, "alpha")
            ok2 = pool.submit(daemon.run_warm, "beta")
            w, r1, r2 = wedged.result(timeout=10), ok1.result(timeout=10), ok2.result(timeout=10)
        ck("the wedged turn fails to its own timeout error (no text)", w["text"] is None and w["error"])
        ck("the healthy concurrent turns still complete", {r1["text"], r2["text"]} == {"reply:alpha", "reply:beta"})
        ck("the warm server survived the wedged turn", daemon.running())
    finally:
        daemon.shutdown()


def test_refusal_is_a_hard_tool_error():
    daemon = new_daemon()
    try:
        r = daemon.run_warm("refuse")
        ck("an ACP refusal stopReason is a hard tool_error, not content",
           r["text"] is None and r["error"] and r.get("tool_error") is True)
    finally:
        daemon.shutdown()


def test_server_initiated_request_is_declined_not_hung():
    daemon = new_daemon()
    try:
        r = daemon.run_warm("permreq", timeout=6)
        ck("a server→client request is declined so the turn completes (never hangs)", r["text"] == "reply:permreq")
    finally:
        daemon.shutdown()


def test_dead_pipe_wakes_every_waiter():
    daemon = new_daemon()
    try:
        process = daemon.ensure_running()
        waiters = [daemon._register_waiter(10_000 + index) for index in range(2)]
        process.terminate()
        process.wait(timeout=5)
        ck("every pending waiter is woken on a dead pipe",
           all(w["event"].wait(3) for w in waiters) and all(w["dead"] for w in waiters))
    finally:
        daemon.shutdown()


if __name__ == "__main__":
    for test in (test_agent_message, test_concurrent_turns_are_demultiplexed, test_n_prompts_one_cold_start,
                 test_wedged_turn_fails_only_itself_others_survive, test_refusal_is_a_hard_tool_error,
                 test_server_initiated_request_is_declined_not_hung, test_dead_pipe_wakes_every_waiter):
        test()
    print(f"\n{'[FAIL]' if fails else 'OK'} test_kimi_daemon_acp_server: {len(fails)} failure(s)")
    sys.exit(1 if fails else 0)

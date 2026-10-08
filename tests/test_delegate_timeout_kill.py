"""(b) The delegate hard-timeout-kill: a hung codex turn must FAIL FAST, not stall the next delegation.

Two gaps this guards against, both found by grounding the warm-daemon path (codex_daemon.run_warm):
  1. run_warm hardcoded CALL_TIMEOUT_S and IGNORED the caller's timeout — so a long agentic delegation got the short
     meta deadline and a `--timeout` was silently dropped. Now the caller's timeout wins; CALL_TIMEOUT_S is only the
     fallback when none is given.
  2. On a timeout, run_warm abandoned the turn but LEFT the app-server running it — the shared daemon stayed wedged and
     the next delegation contended with it (the observed "stall"). recycle_on_timeout now HARD-kills (shutdown →
     respawn) so the next call is fresh. The default stays fail-only-this-call for the concurrent meta-fan.
  3. _route_agentic wires recycle_on_timeout=True and a generous config'd default timeout for an agentic codex route,
     while an explicit caller timeout still wins.

Offline: the real timeout path runs (the waiter event is never set), with ensure_running/_write_rpc stubbed so no
codex process is spawned. Zero spend, no network."""
import os
import sys
import tempfile
import time

os.environ["SPENDGUARD_TEST_ISOLATED"] = "1"
os.environ["SPENDGUARD_NO_AUTOINSTALL"] = "1"
os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="sg-tmo-")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from spendguard import codex_daemon, delegate_router  # noqa: E402

fails = []
def ck(label, cond):
    print(f"  [{'OK' if cond else 'FAIL'}] {label}")
    if not cond:
        fails.append(label)


def _fresh_daemon():
    """A real _CodexDaemon with spawn + stdin WRITE stubbed out — so run_warm exercises the genuine timeout path
    (its waiter event is never set) without ever touching a codex process."""
    d = codex_daemon._CodexDaemon()
    d.ensure_running = lambda: object()        # a truthy 'proc'; the real waiter/timeout machinery still runs
    d._write_rpc = lambda p, obj: None         # the RPC write is a no-op (no real stdin)
    d._cancel_idle_shutdown = lambda: None      # keep the idle timers inert in-test
    d._schedule_idle_shutdown = lambda: None
    return d


# ── 1. a caller timeout is HONORED on the warm path (not the fixed CALL_TIMEOUT_S) ──
d = _fresh_daemon()
d.shutdown = lambda: None                        # not under test here
t0 = time.time()
r = d.run_warm("hi", timeout=0.05)               # 50ms deadline → times out at once
elapsed = time.time() - t0
ck("a tiny caller timeout returns promptly (the 0.05s deadline was used, not 180s)", elapsed < 5.0)
ck("the timeout error reports the caller's deadline, not the fixed CALL_TIMEOUT_S (180)",
   "timeout" in (r.get("error") or "") and "180" not in (r.get("error") or ""))

# ── 2. timeout=None falls back to CALL_TIMEOUT_S in the message (the documented default) ──
# (use a monkeypatched tiny CALL_TIMEOUT_S so the test is fast but still proves the FALLBACK branch)
_orig_call_timeout = codex_daemon.CALL_TIMEOUT_S
codex_daemon.CALL_TIMEOUT_S = 0.05
try:
    d2 = _fresh_daemon()
    d2.shutdown = lambda: None
    r2 = d2.run_warm("hi", timeout=None)
    ck("timeout=None uses CALL_TIMEOUT_S as the fallback deadline", "timeout" in (r2.get("error") or ""))
finally:
    codex_daemon.CALL_TIMEOUT_S = _orig_call_timeout

# ── 3. recycle_on_timeout=True HARD-kills the daemon on a timeout; the default does NOT ──
d3 = _fresh_daemon()
killed = []
d3.shutdown = lambda: killed.append(1)
d3.run_warm("hi", timeout=0.05, recycle_on_timeout=True)
ck("recycle_on_timeout=True calls shutdown() (the wedged daemon is recycled)", killed == [1])

d4 = _fresh_daemon()
not_killed = []
d4.shutdown = lambda: not_killed.append(1)
d4.run_warm("hi", timeout=0.05, recycle_on_timeout=False)
ck("the default (recycle_on_timeout=False) does NOT shut the daemon down (meta-fan keeps the server)", not_killed == [])

# ── 4. _route_agentic wires recycle_on_timeout=True + a generous default timeout for a codex agentic route ──
captured = {}
def _fake_run_prompt(prompt, **kwargs):
    captured.clear()
    captured.update(kwargs)
    return {"text": "ok", "in_tok": 1, "out_tok": 1, "error": None}

import spendguard.codex_exec as _cx        # noqa: E402
_orig_rp = _cx.run_prompt
_orig_spec = delegate_router.lane_registry.lane_spec
delegate_router.lane_registry.lane_spec = lambda plan: {"exec": "codex_exec", "provider": "openai"}
_cx.run_prompt = _fake_run_prompt
try:
    delegate_router._route_agentic("codex", "do a thing", "gpt-5.6-sol", None, "/tmp/repo")
    ck("agentic codex route sets recycle_on_timeout=True", captured.get("recycle_on_timeout") is True)
    ck("agentic codex route with no caller timeout applies the config'd agentic default (not None, not the meta 180)",
       captured.get("timeout") == float(delegate_router.AGENTIC_DELEGATE_TIMEOUT_S) and captured["timeout"] > 180)

    delegate_router._route_agentic("codex", "do a thing", "gpt-5.6-sol", 600, "/tmp/repo")
    ck("an EXPLICIT caller timeout wins over the agentic default", captured.get("timeout") == 600)
finally:
    _cx.run_prompt = _orig_rp
    delegate_router.lane_registry.lane_spec = _orig_spec

print(f"\n{'[FAIL]' if fails else 'OK'} test_delegate_timeout_kill: {len(fails)} failure(s)")
sys.exit(1 if fails else 0)

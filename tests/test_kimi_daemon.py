"""Warm Kimi lane (kimi_daemon) — LIFECYCLE + kimi_exec fallback. Offline, no real kimi, no model spend.

The ACP PROTOCOL flow (initialize → session/new → session/prompt, session/update demux, concurrency, the
N-prompts-one-cold-start pool property, wedged-turn isolation, refusal, dead-pipe wakeups) is covered by
test_kimi_daemon_acp_server.py against a fake `kimi acp`. THIS file guards the protocol-agnostic behaviour: _spawn
degrades (never raises) on an unstartable binary; the warm daemon is default-on with an env opt-out; and
kimi_exec.run_prompt falls back to a cold `kimi -p` when the warm daemon path FAILS — so enabling the daemon changes
LATENCY, never AVAILABILITY.
"""
import os
import sys
import tempfile

os.environ.setdefault("SPENDGUARD_HOME", tempfile.mkdtemp(prefix="sg-kimid-"))
os.environ.setdefault("SPENDGUARD_TEST_ISOLATED", "1")
os.environ.setdefault("SPENDGUARD_NO_AUTOINSTALL", "1")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from spendguard import kimi_daemon as kd        # noqa: E402
from spendguard import kimi_exec as _kx          # noqa: E402


def ck(name, cond):
    ok = bool(cond)
    print(("  [OK] " if ok else "  [FAIL] ") + name)
    return [] if ok else [name]


fails = []

print("-- _spawn returns None (never raises) when the binary can't start --")
_o_bin = _kx._bin
try:
    _kx._bin = lambda: "/nonexistent/kimi-does-not-exist"
    fails += ck("_spawn → None on an unstartable binary", kd._KimiAcpDaemon()._spawn() is None)
finally:
    _kx._bin = _o_bin

print("\n-- daemon is default-on, opt-out works, and a daemon failure falls back to cold `kimi -p` --")
_o_daemon, _o_elig, _o_runwarm, _o_bin2, _o_run = (
    _kx._daemon_enabled, _kx._warm_model_eligible, kd.run_warm, _kx._bin, _kx.subprocess.run)
_old_env = os.environ.pop("SPENDGUARD_KIMI_DAEMON", None)
try:
    fails += ck("daemon is enabled by default", _kx._daemon_enabled())
    os.environ["SPENDGUARD_KIMI_DAEMON"] = "0"
    fails += ck("SPENDGUARD_KIMI_DAEMON=0 opts out", not _kx._daemon_enabled())
    os.environ.pop("SPENDGUARD_KIMI_DAEMON")

    def _boom(*a, **k):
        raise RuntimeError("boom")
    kd.run_warm = _boom                         # the warm path raises → run_prompt must catch + fall to cold `kimi -p`
    _kx._warm_model_eligible = lambda model: True   # force the warm path to be attempted (so the raise is exercised)
    _kx._bin = lambda: "/fake/kimi"

    def _cold_ok(cmd, **kwargs):
        return type("ColdResult", (), {
            "returncode": 0, "stdout": '{"role":"assistant","content":"cold answer"}\n', "stderr": ""})()
    _kx.subprocess.run = _cold_ok
    _out = _kx.run_prompt("hi", model=None)
    fails += ck("warm daemon exception degrades cleanly to cold `kimi -p`",
                _out.get("text") == "cold answer" and not _out.get("error"))
finally:
    (_kx._daemon_enabled, _kx._warm_model_eligible, kd.run_warm, _kx._bin, _kx.subprocess.run) = (
        _o_daemon, _o_elig, _o_runwarm, _o_bin2, _o_run)
    if _old_env is not None:
        os.environ["SPENDGUARD_KIMI_DAEMON"] = _old_env
    else:
        os.environ.pop("SPENDGUARD_KIMI_DAEMON", None)

print("\n-- a specific non-default model is NOT warm-eligible (cold -m path preserves model fidelity) --")
_o_default, _o_alias = _kx._kimi_default_model, _kx._kimi_model_alias
try:
    _kx._kimi_default_model = lambda: "kimi-code/kimi-for-coding"
    _kx._kimi_model_alias = lambda model: ("kimi-code/kimi-for-coding" if model == "dflt" else
                                           ("kimi-code/k3" if model else None))
    fails += ck("unpinned model → warm eligible", _kx._warm_model_eligible(None) is True)
    fails += ck("default model → warm eligible", _kx._warm_model_eligible("dflt") is True)
    fails += ck("specific other model → NOT warm eligible (goes cold with -m)", _kx._warm_model_eligible("k3") is False)
    _kx._kimi_default_model = lambda: None
    fails += ck("unknown default → NOT warm eligible (can't prove equality)", _kx._warm_model_eligible(None) is False)
finally:
    _kx._kimi_default_model, _kx._kimi_model_alias = _o_default, _o_alias

print(f"\n{'[FAIL]' if fails else 'OK'} test_kimi_daemon: {len(fails)} failure(s)")
sys.exit(1 if fails else 0)

"""Reliability check is BOUNDED + the flaky lane fails fast to metered — so a user gets a timely status.

The wedge: the sweep waited a lane's full work-timeout (agy's 300s) for a reachability ping, so ONE dead lane made
the whole check crawl. And a normal call to a flaky lane (agy) waited the 150s global floor before degrading to the
metered API. Guards:
  • lanes.probe(timeout_s=N) passes `timeout=N` to each lane's run_prompt (a dead lane fails in N, not 300s), and
    runs the lanes concurrently;
  • reliability.sweep(run=True, timeout_s=N) bounds every metered ping with `timeout_s=N` and returns a per-resource
    {reachable, latency, …} matrix;
  • reliability._metered_target prefers a model the config actually depends on (so kimi-k3 gets checked, not the
    cheapest served kimi);
  • the per-lane floor: antigravity_exec.MIN_TIMEOUT_S is short (agy fails fast) while the global floor stays high.
Offline: lane mods, lanes.probe, and adapters.call are stubbed; no network, no spend.
"""
import os
import sys
import tempfile

os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="sg-rel-")
os.environ.setdefault("SPENDGUARD_TEST_ISOLATED", "1")
os.environ.setdefault("SPENDGUARD_NO_AUTOINSTALL", "1")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from spendguard import lanes, reliability, adapters, antigravity_exec, model_preflight, gate

fails = []


def ck(name, cond):
    print(("  [OK] " if cond else "  [FAIL] ") + name)
    if not cond:
        fails.append(name)


# ── lanes.probe(timeout_s=N) bounds each lane's run_prompt ──
class _FakeMod:
    def __init__(self):
        self.seen = {}

    def run_prompt(self, prompt, **kw):
        self.seen = kw
        return {"text": "ok", "latency": 0.1}


_fm = _FakeMod()
lanes._lane_mods = lambda: {"testlane": _fm}
lanes.lanes_status = lambda: {"lanes": [{"lane": "testlane", "enabled": True}]}
lanes._record_probe = lambda lane, ok: None
lanes._PROBE_TIER = {}
res = lanes.probe(timeout_s=7)
ck("lanes.probe passes the tight timeout to the lane's run_prompt", _fm.seen.get("timeout") == 7)
ck("lanes.probe reports the lane reachable", res and res[0].get("ok") is True and res[0].get("lane") == "testlane")

# ── antigravity_exec declares a SHORT per-lane floor (agy fails fast), well under the global 150s ──
ck("agy declares a short MIN_TIMEOUT_S floor (fail fast → metered)",
   getattr(antigravity_exec, "MIN_TIMEOUT_S", 999) <= 60 and antigravity_exec.MIN_TIMEOUT_S < adapters.LANE_MIN_TIMEOUT_S)

# ── reliability.sweep(run=True, timeout_s=N) bounds every metered ping and returns the matrix ──
reliability.plan = lambda: {"lanes": [], "metered": [("openai", "gpt-probe")]}
reliability.sweep_estimate = lambda pl=None: {"metered_cost": 0.0, "rows": [], "n_lanes": 0, "n_metered": 1}
import spendguard.lanes as _lmod
_lmod.probe = lambda timeout_s=None: []                       # lanes half stubbed empty
_seen_call = {}


def _fake_call(model, prompt, **kw):
    _seen_call.update(kw)
    return {"error": None, "cost": 0.01, "executor": "api"}


adapters.call = _fake_call
s = reliability.sweep(run=True, timeout_s=9)
ck("sweep bounds the metered ping with timeout_s", _seen_call.get("timeout_s") == 9)
ck("sweep reports the metered provider reachable, with latency", s["metered"]["openai"]["reachable"] is True and "latency" in s["metered"]["openai"])

# ── run=False is a $0 estimate (no probe) ──
_seen_call.clear()
s0 = reliability.sweep(run=False)
ck("sweep(run=False) makes no probe call ($0 estimate)", not _seen_call and s0["metered"] == {} and s0["lanes"] == {})

# ── _metered_target PREFERS a model the config actually uses (kimi-k3), not the cheapest ──
model_preflight.configured_specs = lambda: ["moonshot:kimi-k3", "openai:gpt-5.5"]
gate._provider_of = lambda spec: ("moonshot" if "kimi" in spec else "openai")
ck("_metered_target prefers the configured model for the provider (kimi-k3)", reliability._metered_target("moonshot") == "kimi-k3")

print(("[OK]" if not fails else "[FAIL]") + " reliability bounded: %d failure(s)" % len(fails))
sys.exit(1 if fails else 0)

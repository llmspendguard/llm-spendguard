"""GUARD — metered liveness reflects the RAW METERED KEY, not a subscription-lane route. A metered_only consumer
(honestreview's validate/refute validators must ride the raw metered API for a concurrency-invariant verdict)
preflights on this; if a lane-routed probe green-lit a stale key, every validator would 401 at runtime and the
validators that DID answer are wasted (all findings → UNVALIDATED). Observed 2026-09-18/19: the openai probe's
executor flipped api→codex while the raw key stayed stale, yet health went green.

Pins: (1) sweep() probes a metered provider with metered_only=True (exercises the KEY, not the lane); (2) a probe
that RODE A LANE (served_by_metered_api False) does NOT mark the key verified, and surfaces a reason; (3) a raw-API
success verifies it; (4) a 401 does not; (5) metered_key_ok() exposes the cached verdict — True / False / None.

Hermetic: adapters.call + lanes.probe + plan/sweep_estimate stubbed; no network."""
import os
import sys
import tempfile

os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="spendguard-metkey-")
os.environ.setdefault("SPENDGUARD_TEST_ISOLATED", "1")
os.environ.setdefault("SPENDGUARD_NO_AUTOINSTALL", "1")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from spendguard import reliability, adapters, lanes   # noqa: E402

fails = []


def ck(name, cond):
    print(("  [OK] " if cond else "  [FAIL] ") + name)
    if not cond:
        fails.append(name)


# isolate sweep: one metered provider, no lanes, trivial estimate
reliability.plan = lambda: {"estimate": {}, "lanes": [], "metered": [("openai", "gpt-x")]}
reliability.sweep_estimate = lambda pl=None: {}
lanes.probe = lambda timeout_s=None: []

_seen = {}


def _stub_call(executor, served, error=None):
    def _call(model, prompt, **kw):
        _seen["kw"] = kw
        return {"executor": executor, "served_by_metered_api": served, "error": error,
                "truncated": False, "cost": (0.0001 if served else 0.0)}
    return _call


print("-- sweep probes the RAW metered key (metered_only), and only the API path verifies it --")
adapters.call = _stub_call("api", True)
sw = reliability.sweep(run=True)
ck("sweep pings the metered provider with metered_only=True (the raw key, never the lane)",
   _seen["kw"].get("metered_only") is True)
ck("a raw-API success VERIFIES the key (reachable=True)", sw["metered"]["openai"]["reachable"] is True)

print("\n-- a LANE-routed probe does NOT verify the raw key (the exact bug) --")
adapters.call = _stub_call("codex", False)        # rode the lane despite the request → key un-exercised
sw = reliability.sweep(run=True)
ck("a lane-routed success is NOT counted as key-verified", sw["metered"]["openai"]["reachable"] is False)
ck("... and a reason is surfaced (why it was not verified)", bool(sw["metered"]["openai"]["reason"]))

print("\n-- a 401 on the raw key is not verified --")
adapters.call = _stub_call("api", True, error="AuthenticationError")
sw = reliability.sweep(run=True)
ck("a 401 on the raw key → NOT verified", sw["metered"]["openai"]["reachable"] is False)

print("\n-- metered_key_ok() exposes the cached verdict for a metered_only preflight --")
ck("no recent check → None (UNKNOWN, never assume valid)", reliability.metered_key_ok("openai") is None)
adapters.call = _stub_call("api", True)
reliability._persist_health(reliability.sweep(run=True))
ck("after a verified raw-key probe → True", reliability.metered_key_ok("openai") is True)
adapters.call = _stub_call("codex", False)
reliability._persist_health(reliability.sweep(run=True))
ck("after a lane-routed probe → False (a stale key is never green-lit by a lane)",
   reliability.metered_key_ok("openai") is False)

print(f"\n{'[FAIL]' if fails else 'OK'} test_metered_key_liveness: {len(fails)} failure(s)")
sys.exit(1 if fails else 0)

"""Two storm-coalescer correctness fixes (measured 2026-10-10, surfaced by a graph-vs-text bakeoff whose text arm hit
NO-REPLY on large replies):

  A. The urgency HORIZON must NOT be the per-call reply timeout. storm_route sized the realtime executor's timeout from
     the 30s horizon, so a reply that legitimately took longer was killed → rerouted to batch → came back text=None
     (NO REPLY on LARGE replies). Fix: the executor gets timeout_s=None so adapters.call derives the real per-prompt,
     output-budget-aware deadline from deadline_for; the horizon sizes realtime CAPACITY only.

  B. A coalescer ERROR result must NOT be returned as a COMPLETED call. adapters.call returned every non-None routed
     result early, bypassing failure-ledger recording — a NO-REPLY looked like a completed (empty) call and was recorded
     nowhere. Fix: a routed SUCCESS returns home; a routed ERROR is recorded as a FAILED outcome (disposition='failed')
     and returned as the failure it is — never re-run (double-spend-safe, the coalescer already billed), never counted
     as completed.

Offline, isolated HOME, no network, no real LLM — storm_route.route and calls.record_call are monkeypatched."""
import os
import sys
import tempfile

os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="sg-storm-tf-")
os.environ.setdefault("SPENDGUARD_TEST_ISOLATED", "1")
os.environ.setdefault("SPENDGUARD_NO_AUTOINSTALL", "1")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

import spendguard  # noqa: E402
spendguard.require = lambda: None
from spendguard import adapters, storm_route, calls as _calls  # noqa: E402

MODEL = "anthropic:claude-haiku-4-5"
fails = []
def ck(label, cond):
    print(f"  [{'OK' if cond else 'FAIL'}] {label}")
    if not cond:
        fails.append(label)

# ── A. storm_route builds the realtime executor with timeout_s=None (NOT the horizon) ──
captured_timeout = []
_real_gre = storm_route.governed_realtime_executor
def _spy_gre(model, system, reasoning, intent, timeout_s):
    captured_timeout.append(timeout_s)
    return _real_gre(model, system, reasoning, intent, timeout_s)
storm_route.governed_realtime_executor = _spy_gre
storm_route.dispatch.rate_per_s = lambda *a, **k: 40.0      # non-None rate so get_coalescer builds a coalescer
storm_route.reset_registry()
try:
    c = storm_route.get_coalescer("anthropic", MODEL, "t:storm-a", system="S", reasoning="minimal", horizon_s=30.0)
    ck("get_coalescer built a coalescer (rate known)", c is not None)
    ck("realtime executor timeout is None — NOT the 30s horizon (large replies no longer killed)",
       captured_timeout == [None])
finally:
    storm_route.reset_registry()
    storm_route.governed_realtime_executor = _real_gre

# ── B. a coalescer ERROR is recorded as FAILED and surfaced; a SUCCESS returns home with no failure record ──
rec = {}
_real_record = _calls.record_call
_calls.record_call = lambda *a, **k: rec.update(called=True, disposition=k.get("disposition"))
try:
    # SUCCESS → returned home, NOT recorded as a failure
    storm_route.route = lambda *a, **k: {"text": "an answer", "provider": "anthropic", "model": MODEL}
    rec.clear()
    r_ok = adapters.call(MODEL, "prompt", intent="t:storm-b", metered_only=True)
    ck("routed SUCCESS returned home", isinstance(r_ok, dict) and r_ok.get("text") == "an answer" and not r_ok.get("error"))
    ck("routed SUCCESS not recorded as a failure", not rec.get("called"))

    # ERROR (NO REPLY) → surfaced as a failure AND recorded disposition='failed' (never a silent completed call)
    storm_route.route = lambda *a, **k: {"text": None, "error": "coalescer: timeout", "provider": "anthropic",
                                         "model": MODEL, "status_code": None}
    rec.clear()
    r_err = adapters.call(MODEL, "prompt", intent="t:storm-b", metered_only=True)
    ck("routed ERROR surfaced as a failure (text None, error set)", r_err.get("text") is None and r_err.get("error"))
    ck("routed ERROR recorded as FAILED (not a completed call)",
       rec.get("called") is True and rec.get("disposition") == "failed")
finally:
    _calls.record_call = _real_record

print(f"\n{'[FAIL]' if fails else 'OK'} test_storm_coalescer_timeout_and_failure: {len(fails)} failure(s)")
sys.exit(1 if fails else 0)

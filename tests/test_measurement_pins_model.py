"""measurement=True pins the model (no_substitution), so a bakeoff/panel/A-B is served by the model it NAMED.

Regression for caller-feedback #4: a bakeoff call naming gpt-5-nano was served claude-opus-4-8 via a lane (~100x the
price), and only a log line recorded the swap — so the comparison silently collapsed to one vendor. adapters.call now
treats a call as a measurement (⟹ no_substitution, pinned BEFORE the best-value default / utilisation bandit) when the
caller passes the EXPLICIT flag measurement=True.

It is explicit on PURPOSE: "does this free-form intent denote a measurement?" is a MEANING judgement, and deciding it
by keyword-matching the intent ('bakeoff'/'measurement') is wrong for intents like 'measurement-conversion' and would
force no_substitution there, blocking a permitted fallback. So the flag is declared, never inferred — and this test
LOCKS that a measurement-shaped intent alone does NOT pin.

Offline: _call_once and vendor_call.served_substitute are stubbed; no network, no spend. Isolated SPENDGUARD_HOME, and
the queue + dispatch governor are disabled so the call reaches _call_once directly.
"""
import os
import sys
import tempfile

if not os.environ.get("SPENDGUARD_TEST_ISOLATED"):
    os.environ["SPENDGUARD_TEST_ISOLATED"] = "1"
    os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="sg-measurement-")
    os.environ["SPENDGUARD_ROUTE_THROUGH_QUEUE"] = "0"   # skip the durable queue wrapper
    os.environ["SPENDGUARD_DISPATCH_OFF"] = "1"          # skip the admission governor
    os.execv(sys.executable, [sys.executable] + sys.argv)

from spendguard import adapters, calls, vendor_call  # noqa: E402


class Checks:
    """Accumulates failures on its OWN instance (self), so there is no module-level mutable counter."""
    def __init__(self):
        self.fails = []

    def __call__(self, label, cond, extra=""):
        if not cond:
            self.fails.append(label)
        print(f"  [{'OK' if cond else 'FAIL'}] {label}{('  — ' + extra) if extra and not cond else ''}")


ck = Checks()

# ── capture the no_substitution that actually reaches _call_once ───────────────────────────────────────────────────
captured = {}


def _fake_call_once(model, prompt, **kw):
    captured["no_sub"] = kw.get("_no_sub")
    raw = model.split(":", 1)[-1]
    return {"provider": model.split(":", 1)[0], "model": raw, "text": "ok", "in_tok": 1, "out_tok": 1,
            "latency": 0.0, "cost": 0.0, "finish_reason": "stop", "error": None, "executor": "metered"}


adapters._call_once = _fake_call_once
vendor_call.served_substitute = lambda prov, raw: (None, "")   # no model-resolution swap (keeps it offline)
MODEL = "openai:gpt-5-nano"


def no_sub_for(**call_kwargs):
    captured.clear()
    adapters.call(MODEL, "hi", _no_guard=True, **call_kwargs)
    return captured.get("no_sub")

# the fix: an explicit measurement flag pins the named model
ck("measurement=True pins the model even under a plain intent",
   no_sub_for(intent="concept-category-assignment", measurement=True) is True)

# the doctrine boundary: a measurement-SHAPED intent does NOT auto-pin (no keyword inference of meaning)
ck("a '-bakeoff' intent alone does NOT pin (not inferred from the string)",
   no_sub_for(intent="concept-category-bakeoff") is False)
ck("a 'measurement-...' intent alone does NOT pin (e.g. 'measurement-conversion')",
   no_sub_for(intent="measurement-conversion") is False)

# a plain intent is unaffected (lane routing still allowed)
ck("a plain intent does NOT pin", no_sub_for(intent="concept-category-assignment") is False)

# ambient context with a measurement-shaped intent also does NOT auto-pin — only the explicit flag does
captured.clear()
with calls.context(intent="drug-resolver-bakeoff"):
    adapters.call(MODEL, "hi", _no_guard=True)
ck("an ambient '-bakeoff' context does NOT auto-pin (explicit flag only)", captured.get("no_sub") is False)

captured.clear()
with calls.context(intent="drug-resolver-bakeoff"):
    adapters.call(MODEL, "hi", _no_guard=True, measurement=True)
ck("measurement=True pins even when the ambient intent is unrelated to the flag", captured.get("no_sub") is True)

# an explicit no_substitution=True is obviously honored
ck("explicit no_substitution=True stays pinned", no_sub_for(intent="x", no_substitution=True) is True)

print(f"\n{'OK' if not ck.fails else 'FAIL'} test_measurement_pins_model: {len(ck.fails)} failure(s)")
sys.exit(1 if ck.fails else 0)

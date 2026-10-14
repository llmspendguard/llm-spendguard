"""Offline guard: unmappable realtime models require an explicit unpriced override."""
import os
import sys
import tempfile
import warnings
from unittest.mock import patch

os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="sg-unpriced-hardfail-")
os.environ["SPENDGUARD_NO_AUTOINSTALL"] = "1"
os.environ["TEST_ISOLATED"] = "1"
os.environ["SPENDGUARD_TEST_ISOLATED"] = "1"
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))
# A startup hook may have imported another checkout before isolation was configured.
for name in list(sys.modules):
    if name == "spendguard" or name.startswith("spendguard."):
        del sys.modules[name]

from spendguard import gate, pricing  # noqa: E402

fails = []


def ck(label, condition, detail=""):
    print(f"  [{'OK' if condition else 'FAIL'}] {label}" + (f" — {detail}" if detail and not condition else ""))
    if not condition:
        fails.append(label)


model = "totally-unknown-hardfail-test"
for override in (None, "0"):
    with patch.dict(os.environ):
        if override is None:
            os.environ.pop("GATE_ALLOW", None)
        else:
            os.environ["GATE_ALLOW"] = override
        with patch.object(pricing, "realtime_cost", side_effect=pricing.PriceUnknownError(model)), \
                patch.object(gate, "_emit") as emitted:
            try:
                gate._rt_precheck("openai", model, 100, 10)
                ck(f"unknown refused with GATE_ALLOW={override}", False)
            except gate.SpendGateRefused as exc:
                ck(f"unknown refused with GATE_ALLOW={override}", True)
                ck("refusal explains resolution", all(s in str(exc) for s in (model, "model_catalog", "GATE_ALLOW")))
            except Exception as exc:
                ck("unknown raises SpendGateRefused", False, str(exc))
            ck("unpriced refusal event", emitted.call_args_list == [(({
                "kind": "realtime", "provider": "openai", "model": model,
                "cost": None, "decision": "refused_unpriced",
            },), {})])

for label, override, error in (
    ("explicit override", "1", pricing.PriceUnknownError(model)),
    ("transient pricing error", "0", RuntimeError("temporary pricing failure")),
    ("priced model", "0", None),
):
    with patch.dict(os.environ, {"GATE_ALLOW": override}):
        # Pricing is the only stub on these paths; the real USD/budget precheck uses the temp ledger.
        with patch.object(pricing, "realtime_cost", side_effect=error, wraps=pricing.realtime_cost), \
                warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            try:
                gate._rt_precheck("openai", "gpt-6.1-sol" if error is None else model, 100, 10)
                ck(f"{label} proceeds", True)
            except Exception as exc:
                ck(f"{label} proceeds", False, str(exc))
            if error is not None:
                ck(f"{label} warns UNPRICED", any("UNPRICED" in str(w.message) for w in caught))
            if override == "1":
                ck("override warning names GATE_ALLOW", any("GATE_ALLOW" in str(w.message) for w in caught))

print(f"\n{'[FAIL]' if fails else 'OK'} test_gate_unpriced_hardfail: {len(fails)} failure(s)")
sys.exit(1 if fails else 0)

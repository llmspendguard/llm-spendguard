import os
import sys
import tempfile
from unittest.mock import patch
os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="sg-e-")
os.environ["SPENDGUARD_NO_AUTOINSTALL"] = "1"
os.environ["TEST_ISOLATED"] = "1"
os.environ["SPENDGUARD_TEST_ISOLATED"] = "1"
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))
for name in list(sys.modules):
    if name == "spendguard" or name.startswith("spendguard."):
        del sys.modules[name]
from spendguard import adapters, bulkgate, gate  # noqa: E402
fails = []
def ck(label, condition):
    print(f"{'OK' if condition else 'FAIL'} {label}")
    if not condition:
        fails.append(label)

from types import SimpleNamespace as NS  # noqa: E402
usage = NS(usage=NS(output_tokens_details=NS(reasoning_tokens=31996)))
ck("reported reasoning", gate._rt_reasoning_report(usage) == 31996)
ck("absent remains unknown", gate._rt_reasoning_report(NS()) is None)
with patch.object(gate, "_gate_warn_once") as warning:
    gate._record_rt("gemini-3.8-flash", {"max_output_tokens": 32000}, 100, 32000,
                    finish="length", cost=0, reasoning_tokens=gate._rt_reasoning_report(usage))
    ck("warned", warning.called and "REASONING_STARVED" in warning.call_args[0][0])
row = bulkgate._gate_calls_db().execute("SELECT disposition,reasoning_tokens,visible_tokens FROM gate_calls").fetchone()
ck("starvation fact", tuple(row) == ("reasoning_starved", 31996, 4))
with patch.object(gate, "_gate_warn_once") as warning:
    bulkgate.note_response("normal", "gemini-3.8-flash", 500, 32000, "stop", reasoning_tokens=100)
    ck("normal unflagged", not warning.called)
with patch.object(gate, "_gate_warn_once") as warning:
    bulkgate.note_response("unknown", "x", 4, 32000, "length")
    ck("weaker evidence explicit", "weaker" in warning.call_args[0][0])
ck("bounded reasoning floor", adapters.output_budget("gemini-3.8-flash", bounded_structured=True) >= adapters.BOUNDED_REASONING_OUTPUT_FLOOR)
ck("ordinary unchanged", adapters.output_budget("gpt-4.1-nano", bounded_structured=True) == adapters.output_budget("gpt-4.1-nano"))
original_cfg = adapters.config._cfg_get
with patch.object(adapters.config, "_cfg_get", side_effect=lambda section, key, default=None: 1000000 if key == "bounded_reasoning_output_floor" else original_cfg(section, key, default)):
    try:
        adapters.output_budget("gemini-3.8-flash", bounded_structured=True)
        ck("impossible floor refused", False)
    except ValueError:
        ck("impossible floor refused", True)
sys.exit(1 if fails else 0)

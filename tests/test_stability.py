"""measure_stability: run-to-run variance at PRODUCTION settings (replicates), the honest characterization of lane/
screen stability that temperature=0 cannot give. Offline — adapters.call is stubbed to return controlled per-run
outputs (a STABLE prompt that answers the same every run, and a FLIPPY prompt that varies), so the variance maths is
asserted without any network or spend."""
import os
import sys
import tempfile

os.environ["SPENDGUARD_TEST_ISOLATED"] = "1"
os.environ["SPENDGUARD_NO_AUTOINSTALL"] = "1"
os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="sg-stability-")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from spendguard import stability, adapters  # noqa: E402

fails = []
def ck(label, cond):
    print(f"  [{'OK' if cond else 'FAIL'}] {label}")
    if not cond:
        fails.append(label)

# Deterministic STUB: prompt 0 ("stable") always → "home"; prompt 1 ("flippy") → "home" on even runs, "nothome" on odd.
# We track an internal call counter so the flippy prompt varies run-to-run exactly as a nondeterministic screen would.
_state = {"calls": 0}
def _fake_call(model, prompt, intent=None, system=None, reasoning=None, **kw):
    _state["calls"] += 1
    if prompt == "stable":
        text = "DECISION: home"
    else:  # "flippy": alternates each time it's seen, like a nondeterministic screen
        text = "DECISION: home" if (_state["flippy_seen"] % 2 == 0) else "DECISION: nothome"
        _state["flippy_seen"] += 1
    return {"text": text, "cost": 0.0, "executor": "codex", "error": None, "in_tok": 5, "out_tok": 2,
            "finish_reason": "stop"}
_state["flippy_seen"] = 0
adapters.call = _fake_call

def _parse(txt):                      # parse the DECISION label — format extraction, not a judgement
    return txt.split("DECISION:", 1)[1].strip() if "DECISION:" in txt else txt.strip()

res = stability.measure_stability(["stable", "flippy"], "gpt-6-luna", k=4, intent="onlyhome-screen", parse=_parse)

ck("ran k replicates over each prompt (k*n calls)", _state["calls"] == 4 * 2)
ck("executor reflects the production path ($0 lane), not forced metered", res["executor"] == "codex")
# prompt 0 is stable across all runs; prompt 1 flips
pp = {p["prompt_idx"]: p for p in res["per_prompt"]}
ck("the stable prompt is reported stable (1 distinct outcome)", pp[0]["stable"] and pp[0]["distinct"] == 1)
ck("the flippy prompt is reported UNstable (>1 distinct outcome)", (not pp[1]["stable"]) and pp[1]["distinct"] == 2)
ck("flipped lists exactly the flippy prompt", res["flipped"] == [1])
ck("stable_frac = 1 of 2 prompts stable", abs(res["stable_frac"] - 0.5) < 1e-9)
# rate spread: 'home' rate swings run-to-run because the flippy prompt alternates (home count 2 vs 1 of 2 prompts)
band = res["rate_spread"].get("home", {}).get("band")
ck("rate_spread surfaces a non-zero between-run band for the swinging label", band is not None and band > 0)

# a NON-measurement, NO-knob call path: measure_stability must NOT pass measurement/temperature (production settings)
_seen_kwargs = {}
def _capture_call(model, prompt, **kw):
    _seen_kwargs.clear(); _seen_kwargs.update(kw)
    return {"text": "x", "cost": 0.0, "executor": "codex", "error": None}
adapters.call = _capture_call
stability.measure_stability(["p"], "gpt-6-luna", k=1)
ck("measure_stability sends NO measurement flag (production settings)", "measurement" not in _seen_kwargs)
ck("measure_stability sends NO determinism knobs", not any(k in _seen_kwargs for k in ("temperature", "top_p", "seed")))

print(f"\n{'[FAIL]' if fails else 'OK'} test_stability: {len(fails)} failure(s)")
sys.exit(1 if fails else 0)

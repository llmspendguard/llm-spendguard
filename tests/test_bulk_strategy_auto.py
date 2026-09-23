"""strategy='auto' — the QUOTA-AWARE split (the economic model as one call): fan the UNDER-USED, non-reserved plan
lanes (by MEASURED est-value: idle/warm, never HOT — a hot plan's quota is scarce), and BATCH the overflow (~half
price) so cheap bulk never burns valuable plan quota. It composes existing knobs — derives lanes= from
lane_utilization's idle/warm/HOT state (reused, not a new threshold) + the reserved-lane economics, and defaults
on_miss='batch'. Pins: a HOT plan is never fanned; a RESERVED lane is never fanned; the overflow degrades to batch;
an unknown strategy raises. Offline: fan + economics stubbed, no LLM.
"""
import os
import sys
import tempfile

os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="sg-auto-")
os.environ.setdefault("SPENDGUARD_TEST_ISOLATED", "1")
os.environ.setdefault("SPENDGUARD_NO_AUTOINSTALL", "1")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from spendguard import lane_balance, lane_catalog, lane_bandit, adapters, dispatch, lane_economics   # noqa: E402


def ck(name, cond):
    ok = bool(cond)
    print(("  [OK] " if ok else "  [FAIL] ") + name)
    return [] if ok else [name]


fails = []
ALL = [("codex", "gpt-5.6-luna"), ("gemini", "g-low"), ("claude-code", "haiku"), ("zai-coding", "glm")]
_PROV = {"codex": "openai", "gemini": "gemini", "claude-code": "anthropic", "zai-coding": "zai"}
lane_catalog.arms = lambda flt=None: [(ln, m) for (ln, m) in ALL if not flt or ln in flt]   # HONOR the lane filter (like the real one)
lane_catalog.lane_provider = lambda l: _PROV.get(l)
lane_bandit._arm_cooling = lambda l, u: False
adapters._lane_cooling = lambda l: False
lane_bandit.arm_stats = lambda intent: {a: {"winrate": 1.0, "trials": 2} for a in ALL}
dispatch.acquire = lambda *a, **k: 0.0
dispatch.release = lambda *a, **k: None

# MEASURED economics: codex IDLE, gemini WARM, claude-code HOT (scarce), zai WARM but RESERVED (held for real coding).
lane_balance.lane_utilization = lambda: {"lanes": [
    {"lane": "codex", "state": "idle"}, {"lane": "gemini", "state": "warm"},
    {"lane": "claude-code", "state": "hot"}, {"lane": "zai-coding", "state": "warm"}]}
lane_economics.prompt_lane_reserved = lambda l: l == "zai-coding"

_served = []


def _call(model, prompt, max_tokens=None, system=None, reasoning=None, schema=None, timeout_s=None, sig=None,
          retries=2, files=None, _no_guard=False, no_metered_fallback=False, images=None, no_substitution=False, **kw):
    prov, raw = model.split(":", 1)
    lane = {v: k for k, v in _PROV.items()}.get(prov)
    _served.append(lane)
    if prompt == "MISS":                                  # a task the fill lanes can't serve → overflows to batch
        return {"text": None, "error": "lane miss", "cost": None}
    return {"text": f"ans::{lane}", "cost": 0, "executor": lane, "provider": prov, "model": raw}


adapters.call = _call
_batched = {"n": 0}


def _submit(misses):
    _batched["n"] += len(misses)
    return "batch-handle-1"


print("-- strategy=auto fans ONLY the under-used, non-reserved plans; HOT (claude-code) + reserved (zai) excluded --")
TASKS = [f"t{i}" for i in range(7)] + ["MISS"]
res = lane_balance.bulk_delegate(TASKS, "auto:test", strategy="auto", batch_submit=_submit,
                                 task_key=lambda t: t, return_keyed=True, force=True)
fails += ck("served ONLY on codex/gemini (a HOT plan's scarce quota + a reserved lane are never burned on bulk)",
            bool(_served) and set(l for l in _served if l) <= {"codex", "gemini"})
fails += ck("the fill lanes answered the servable tasks ($0)",
            all(res[t].get("text") for t in [f"t{i}" for i in range(7)]))

print("\n-- the overflow DEGRADED to batch (on_miss defaulted to 'batch'), not realtime metered --")
fails += ck("the un-servable task was routed to BATCH, not billed realtime",
            res["MISS"].get("reason") == "queued_batch" and res["MISS"].get("batch") == "batch-handle-1")
fails += ck("batch_submit received the overflow (1 miss)", _batched["n"] == 1)

print("\n-- an unknown strategy is refused at the door --")
_raised = False
try:
    lane_balance.bulk_delegate(["x"], "auto:bad", strategy="cheapest", force=True)
except ValueError:
    _raised = True
fails += ck("strategy='cheapest' (unknown) → ValueError", _raised)

print(f"\n{'[FAIL]' if fails else 'OK'} test_bulk_strategy_auto: {len(fails)} failure(s)")
sys.exit(1 if fails else 0)

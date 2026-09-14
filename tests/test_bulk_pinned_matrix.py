"""bulk_delegate as the GOVERNED, NO-SUBSTITUTION matrix fan for a TEXT pinned-vendor panel.

The consensus-diversity workload (N files × M NAMED vendors — e.g. honestreview's repo review): each task must go to
its EXACT vendor (no bandit substitution), the whole matrix submitted with NO caller concurrency number (the governor
decides), results keyed, durable, and every metered call bounded per-vendor by the dispatch governor. Before this,
`model_for` only selected the model on the VISION runner (needed images); a text matrix was ignored on the lane path.
Pins: each task lands on its pinned model; no_substitution reaches the call; served_by_metered_api per row; keyed
results; per-task error containment; dispatch.acquire governs EVERY task. Offline: adapters.call + dispatch stubbed.
"""
import os
import sys
import tempfile

os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="sg-pin-")
os.environ.setdefault("SPENDGUARD_TEST_ISOLATED", "1")
os.environ.setdefault("SPENDGUARD_NO_AUTOINSTALL", "1")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from spendguard import lane_balance, adapters, dispatch   # noqa: E402


def ck(name, cond):
    ok = bool(cond)
    print(("  [OK] " if ok else "  [FAIL] ") + name)
    return [] if ok else [name]


fails = []
_acq = {"n": 0}
dispatch.acquire = lambda *a, **k: (_acq.__setitem__("n", _acq["n"] + 1), 0.0)[1]   # count governed admissions
dispatch.release = lambda *a, **k: None
adapters.provider_for = lambda m: (m.split(":", 1)[0] if ":" in m else "openai")

# the MATRIX: 3 files × 3 named vendors, flattened to (file, vendor) tasks — each pinned to its exact model.
VENDORS = ["openai:gpt-5.5", "anthropic:claude-opus-4-8", "moonshot:kimi-k3"]
TASKS = [{"file": f, "model": m, "prompt": f"review {f}"} for f in ("a.py", "b.py", "c.py") for m in VENDORS]


class _Recorder:
    def __init__(self):
        self.calls = []

    def __call__(self, model, prompt, max_tokens=None, system=None, reasoning=None, schema=None,
                 timeout_s=None, sig=None, retries=2, files=None, _no_guard=False, no_metered_fallback=False,
                 images=None, no_substitution=False, metered_only=False):
        self.calls.append({"model": model, "no_substitution": no_substitution, "images": images,
                           "metered_only": metered_only})
        if model == "moonshot:kimi-k3" and "b.py" in prompt:                # one cell errors — must NOT wedge the matrix
            return {"text": None, "error": "vendor 500", "cost": None}
        prov, raw = model.split(":", 1)
        return {"text": f"ans::{model}", "cost": 0.001, "provider": prov, "model": raw, "executor": "api"}


_rec = _Recorder()
adapters.call = _rec

print("-- a TEXT pinned-vendor matrix rides the governed no-substitution metered runner, keyed by task --")
res = lane_balance.bulk_delegate(
    TASKS, "panel:review", model_for=lambda t: t["model"], prompt_for=lambda t: t["prompt"],
    task_key=lambda t: f"{t['file']}|{t['model']}", return_keyed=True, force=True)

fails += ck("returned a DICT keyed by task_key (return_keyed)", isinstance(res, dict) and len(res) == len(TASKS))
fails += ck("EVERY task landed on its EXACT pinned model (no substitution across the matrix)",
            all(res[f"{t['file']}|{t['model']}"].get("model") == t["model"] for t in TASKS if "b.py" not in t["prompt"] or t["model"] != "moonshot:kimi-k3"))
fails += ck("every served row is served_by_metered_api (rode the metered API, not a lane)",
            all(r.get("served_by_metered_api") for r in res.values() if r.get("text")))
fails += ck("no_substitution=True reached EVERY underlying call (a pinned task never swaps vendor)",
            bool(_rec.calls) and all(c["no_substitution"] is True for c in _rec.calls))
fails += ck("images=None on every call (a TEXT matrix — no image channel)",
            all(c["images"] is None for c in _rec.calls))
fails += ck("the governor admitted EVERY task (dispatch.acquire per task — metered concurrency bounded)",
            _acq["n"] == len(TASKS))

print("\n-- durability: one erroring cell is a NAMED error row, never a wedge; the rest of the matrix succeeds --")
_err = res["b.py|moonshot:kimi-k3"]
fails += ck("the erroring cell is an error row (reason set, no text), not a crash",
            _err.get("text") is None and _err.get("reason") and _err.get("error"))
fails += ck("every OTHER cell still succeeded (the matrix did not wedge)",
            sum(1 for r in res.values() if r.get("text")) == len(TASKS) - 1)

print("\n-- metered_only: default rides the atomic pair (lane-first); metered_only=True forces the METERED half --")
fails += ck("default fan → metered_only=False on every call (atomic pair — $0 lane first, then metered fallback)",
            bool(_rec.calls) and all(c["metered_only"] is False for c in _rec.calls))
_rec.calls.clear()
res_mo = lane_balance.bulk_delegate(
    TASKS, "panel:review", model_for=lambda t: t["model"], prompt_for=lambda t: t["prompt"],
    task_key=lambda t: f"{t['file']}|{t['model']}", return_keyed=True, metered_only=True, force=True)
fails += ck("metered_only=True → metered_only=True reached EVERY underlying call (forces the metered half of the pin)",
            bool(_rec.calls) and all(c["metered_only"] is True for c in _rec.calls))
fails += ck("metered_only fan still keyed + no_substitution on every call (pin intact)",
            isinstance(res_mo, dict) and len(res_mo) == len(TASKS) and all(c["no_substitution"] is True for c in _rec.calls))

print("\n-- no model_for → still the LANE path (unchanged); model_for alone → the pinned metered path --")
# a task whose model_for returns None → a clean per-task 'no_model' row, never a crash
res_nomodel = lane_balance.bulk_delegate(["x"], "panel:nomodel", model_for=lambda t: None, force=True)
fails += ck("model_for returning None → reason=no_model (per-task, re-runnable)",
            res_nomodel[0].get("reason") == "no_model" and res_nomodel[0].get("text") is None)

print(f"\n{'[FAIL]' if fails else 'OK'} test_bulk_pinned_matrix: {len(fails)} failure(s)")
sys.exit(1 if fails else 0)

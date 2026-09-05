"""Durable cross-vendor VISION panel: N vendors label the SAME image, each call checkpointed PER-VENDOR so a crash
mid-panel resumes without re-paying the vendors that already answered.

crossllm.ask() is a synchronous fan (fan_out) that loses paid results on a crash. This panel rides bulk_delegate's
durable core with a per-task model_for + prompt_for: the task is the vendor id (keyed per-vendor via model_for), the
prompt+images are shared. crossllm.ask_vision is the estimate-first wrapper. Offline: adapters.call / _load_image /
_image_input_tokens / pricing / dispatch stubbed — no network, no LLM.
"""
import os
import sys
import tempfile

os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="sg-askvis-")
os.environ.setdefault("SPENDGUARD_TEST_ISOLATED", "1")
os.environ.setdefault("SPENDGUARD_NO_AUTOINSTALL", "1")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from spendguard import lane_balance, adapters, pricing, crossllm, gate as sg

fails = []


def ck(name, cond):
    print(("  [OK] " if cond else "  [FAIL] ") + name)
    if not cond:
        fails.append(name)


dispatch = __import__("spendguard.dispatch", fromlist=["x"])
dispatch.acquire = lambda *a, **k: 0.0
dispatch.release = lambda *a, **k: None
IMG = ["data:image/png;base64,AAAA"]
VENDORS = ["openai:gpt-5-nano", "gemini:g-flash", "zai:glm-v"]

_calls = []


def _fake_call(model, prompt, **kw):
    _calls.append({"model": model, "prompt": prompt, "images": kw.get("images")})
    return {"text": '{"label":"x"}', "cost": 0.003, "executor": "api",
            "model": model.split(":")[1], "provider": model.split(":")[0]}


# ── bulk_delegate(model_for, prompt_for): each vendor runs its OWN model on the SHARED prompt+images ──
adapters.call = _fake_call
_calls.clear()
rows = lane_balance.bulk_delegate(list(VENDORS), "panel", images_for=lambda t: IMG,
                                  model_for=lambda t: t, prompt_for=lambda t: "LABEL THIS IMAGE", force=True)
models = sorted(c["model"] for c in _calls)
ck("one call per vendor, each on its OWN model", models == sorted(VENDORS) and len(_calls) == 3)
ck("every vendor gets the SHARED prompt (prompt_for), not the task id", all(c["prompt"] == "LABEL THIS IMAGE" for c in _calls))
ck("every vendor gets the images", all(c["images"] == IMG for c in _calls))
ck("results are per-vendor, in vendor order", [r["model"] for r in rows] == VENDORS)

# ── per-vendor KEYING + RESUME: the same prompt on 3 vendors → 3 distinct checkpoint entries (not 1 collision),
#    and a re-run with that checkpoint re-pays NOBODY ──
ckpt = os.path.join(os.environ["SPENDGUARD_HOME"], "panel.jsonl")
_calls.clear()
lane_balance.bulk_delegate(list(VENDORS), "panel2", images_for=lambda t: IMG, model_for=lambda t: t,
                           prompt_for=lambda t: "SAME PROMPT", checkpoint=ckpt, chunk_size=3, force=True)
n_first = len(_calls)
lines = [ln for ln in open(ckpt).read().splitlines() if ln.strip()]
ck("3 vendors on ONE shared prompt → 3 distinct checkpoint entries (per-vendor keying, no collision)",
   n_first == 3 and len(lines) == 3)
_calls.clear()
lane_balance.bulk_delegate(list(VENDORS), "panel2", images_for=lambda t: IMG, model_for=lambda t: t,
                           prompt_for=lambda t: "SAME PROMPT", checkpoint=ckpt, chunk_size=3, force=True)
ck("re-run with the checkpoint re-pays NOBODY (all 3 vendors resumed)", len(_calls) == 0)

# ── images_for with NEITHER vision_model nor model_for → every task errors (re-runnable) ──
r_none = lane_balance.bulk_delegate(["x"], "novm", images_for=lambda t: IMG, force=True)
ck("images_for without vision_model or model_for → reason=no_vision_model", r_none[0].get("reason") == "no_vision_model")

# ── crossllm.ask_vision: estimate-first, budget gate, BudgetRefused is a deliberate stop ──
adapters._load_image = lambda i: {"w": 1, "h": 1, "media_type": "image/png", "b64": "x", "data_uri": str(i)}
adapters._image_input_tokens = lambda imgs, prov, raw: 10
pricing.cost_or_unpriced = lambda *a, **k: 0.01              # 0.01 per vendor → 0.03 for the 3-vendor panel

_calls.clear()
est = crossllm.ask_vision("label", IMG, VENDORS)
ck("no budget → estimate only, 0 spend", "estimate_usd" in est and est.get("note") and not _calls)
ck("estimate sums per-vendor (3 × 0.01 = 0.03)", abs(est["estimate_usd"] - 0.03) < 1e-9 and len(est["per_vendor"]) == 3)

_calls.clear()
panel = crossllm.ask_vision("label", IMG, VENDORS, budget_usd=1.0)
ck("budget >= est → runs the panel, one paid call per vendor", panel.get("complete") and len(_calls) == 3 and panel["n_ok"] == 3)

_calls.clear()
refused = None
try:
    crossllm.ask_vision("label", IMG, VENDORS, budget_usd=0.001)
except crossllm.BudgetRefused as e:
    refused = e
ck("est > budget → BudgetRefused BEFORE any spend", refused is not None and not _calls)
ck("...BudgetRefused is a DELIBERATE stop (a SpendGateRefused)", isinstance(refused, sg.SpendGateRefused))

# ── input guards raise (a bug, not a silent no-op) ──
def _raises(fn):
    try:
        fn()
        return False
    except ValueError:
        return True


ck("no vendors → ValueError", _raises(lambda: crossllm.ask_vision("p", IMG, [])))
ck("no images → ValueError", _raises(lambda: crossllm.ask_vision("p", [], VENDORS)))

print(("[OK]" if not fails else "[FAIL]") + " ask vision panel: %d failure(s)" % len(fails))
sys.exit(1 if fails else 0)

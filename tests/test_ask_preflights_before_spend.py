"""spendguard.ask HARD-GATES the model before the full run — the 'test the model before real spend' rail, applied to
every cross-LLM caller. A named model that is not callable AS WRITTEN (stale / unpriced / unknown) is refused with
its fix BEFORE any fan-out, instead of being discovered after paying for a whole run of failures (the gemini-3-flash
→ gemini-3-flash-preview case that wasted a 26-doc batch). The gate is RE-CHECKED on every call (never memoized), so
a model revoked/unpriced mid-process is caught the next call, not skipped on a stale cached OK — cheap because
served_check is $0 cache-first and a bad model stops the batch on the first call.

Hermetic: preflight + fan_out are stubbed, so no network and zero spend; the point is that fan_out is NEVER reached
on a bad model, and reached exactly once per clean call."""
import sys

from spendguard import crossllm
import spendguard.model_preflight as mp
from spendguard.crossllm import ModelPreflightRefused
from spendguard.gate import SpendGateRefused

fails = []
def ck(name, cond):
    print(("  [OK] " if cond else "  [FAIL] ") + name)
    if not cond:
        fails.append(name)

ck("ModelPreflightRefused is a SpendGateRefused (propagates by construction)",
   issubclass(ModelPreflightRefused, SpendGateRefused))

# ── stub fan_out (the only spend path) so a 'proceed' never actually calls a model ──────────────────────────────
_fan_calls = []
def _fake_fan(vlist, prompt, **kw):
    _fan_calls.append(list(vlist))
    return {"results": [], "ok": [], "failed": [], "n": 0, "n_ok": 0, "complete": False, "run_id": "t"}

# ── stub preflight: an EXPLICIT set of specs is not usable (exact match, never a substring heuristic — the real
#    preflight decides by served+priced, not by what the id string contains); count how many times it's consulted ──
_UNUSABLE = {"x:bad-model"}
_pf_calls = []
def _fake_pf(specs, correct=True):
    _pf_calls.append(list(specs))
    return [{"spec": s, "usable": (s not in _UNUSABLE), "served": ("stale" if s in _UNUSABLE else "served"),
             "corrected": None, "priced": True, "note": ("STALE — fix it" if s in _UNUSABLE else "ok")} for s in specs]

_orig_fan, _orig_pf = crossllm.vendor_call.fan_out, mp.preflight_models
crossllm.vendor_call.fan_out = _fake_fan
mp.preflight_models = _fake_pf
try:
    # 1. a BAD (stale) model → refuse, and fan_out is NEVER reached (no spend)
    try:
        crossllm.ask("hi", vendors=["x:bad-model"], preflight=True)
        ck("a bad model is refused (raised)", False)
    except ModelPreflightRefused as e:
        ck("bad model → ModelPreflightRefused with its fix", "STALE" in str(e))
    ck("fan_out NEVER called on a refusal — nothing spent", len(_fan_calls) == 0)

    # 2. a CLEAN set → proceeds to fan_out
    crossllm.ask("hi", vendors=["x:good-model"], preflight=True)
    ck("clean set → fan_out reached (the call proceeds)", len(_fan_calls) == 1)

    # 3. the SAME clean set again → preflight RE-CHECKED (never a stale cached OK), and the call still fans out
    n_pf = len(_pf_calls)
    crossllm.ask("hi again", vendors=["x:good-model"], preflight=True)
    ck("repeated identical vendor-set → RE-CHECKED every call (no stale cached OK)", len(_pf_calls) == n_pf + 1)
    ck("...and the 2nd call still fanned out", len(_fan_calls) == 2)

    # 4. preflight=False opts out (a caller that already validated) — no preflight consulted
    n_pf2 = len(_pf_calls)
    crossllm.ask("hi", vendors=["x:another-model"], preflight=False)
    ck("preflight=False skips the gate entirely", len(_pf_calls) == n_pf2)
finally:
    crossllm.vendor_call.fan_out = _orig_fan
    mp.preflight_models = _orig_pf

print(("\n[OK] " if not fails else "\n[FAIL] ") + "ask_preflights_before_spend: %d failure(s)" % len(fails))
sys.exit(1 if fails else 0)

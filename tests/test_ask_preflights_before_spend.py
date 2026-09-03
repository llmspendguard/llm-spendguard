"""spendguard.ask gates the model before the full run — the 'test the model before real spend' rail, applied to
every cross-LLM caller — but HONEST-PARTIAL, not all-or-nothing. A named model that is not callable AS WRITTEN
(stale / unpriced / unknown) is never called (never wastes a run of failures — the gemini-3-flash →
gemini-3-flash-preview case that burned a 26-doc batch), and it is MARKED in the result (PREFLIGHT_UNMET) so the
caller sees which model was skipped and why. One bad id does NOT refuse the whole batch: the usable models still
answer. The batch is refused (ModelPreflightRefused) only when NOTHING is callable. The gate is RE-CHECKED on every
call (never memoized), so a model revoked/unpriced mid-process is caught the next call.

Hermetic: preflight + fan_out are stubbed, so no network and zero spend; the point is that a bad model is never
handed to fan_out, an all-bad set refuses before fan_out, and a mixed set fans out only the usable models."""
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
    results = [crossllm.vendor_call.Result(crossllm.vendor_call.OK, v, m, text="ok") for v, m in vlist]
    return {"results": results, "ok": list(results), "failed": [], "n": len(results), "n_ok": len(results),
            "complete": True, "run_id": "t"}

# ── stub preflight: an EXPLICIT set of specs is not usable (exact match, never a substring heuristic — the real
#    preflight decides by served+priced, not by what the id string contains); count how many times it's consulted ──
_UNUSABLE = {"x:bad-model", "x:bad-model-2"}
_pf_calls = []
def _fake_pf(specs, correct=True):
    _pf_calls.append(list(specs))
    # mirror the REAL preflight_models row shape — provider + model are what _inject_unmet needs to label the
    # skipped vendor honestly; a mock that omits them would ship an unlabeled '?' coverage row.
    out = []
    for s in specs:
        v, m = s.split(":", 1)
        out.append({"spec": s, "provider": v, "model": m, "usable": (s not in _UNUSABLE),
                    "served": ("stale" if s in _UNUSABLE else "served"), "corrected": None, "priced": True,
                    "note": ("STALE — fix it" if s in _UNUSABLE else "ok")})
    return out

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

    # 5. HONEST PARTIAL: a MIX (usable + bad) → fan out ONLY the usable model (the bad one is never handed to
    #    fan_out, never spent) and MARK the bad one PREFLIGHT_UNMET. One bad id does NOT refuse the whole batch.
    _fan_calls.clear()
    r = crossllm.ask("hi", vendors=["a:good-model", "x:bad-model"], preflight=True)
    ck("mix → fan_out reached with ONLY the usable model (bad one never handed to it)",
       len(_fan_calls) == 1 and _fan_calls[0] == [("a", "good-model")])
    ck("mix → usable model answers, bad one is PREFLIGHT_UNMET in by_vendor (honest, not silently dropped)",
       r.by_vendor.get("a") == crossllm.vendor_call.OK
       and r.by_vendor.get("x") == crossllm.vendor_call.PREFLIGHT_UNMET)
    ck("mix → coverage INCOMPLETE (a requested model was skipped); n counts both, n_ok only the answer",
       r.complete is False and r.n == 2 and r.n_ok == 1)
    ck("mix → the unmet model carries NO text (the Result invariant holds — never a failure-as-answer)",
       all(not res.ok for res in r.results if res.vendor == "x"))

    # 6. ALL bad → STILL refused: there is no partial coverage to give, so the whole batch is refused before spend
    _fan_calls.clear()
    try:
        crossllm.ask("hi", vendors=["x:bad-model", "x:bad-model-2"], preflight=True)
        ck("all-bad set → refused", False)
    except ModelPreflightRefused:
        ck("all-bad set → ModelPreflightRefused (nothing callable), fan_out never reached", len(_fan_calls) == 0)
finally:
    crossllm.vendor_call.fan_out = _orig_fan
    mp.preflight_models = _orig_pf

print(("\n[OK] " if not fails else "\n[FAIL] ") + "ask_preflights_before_spend: %d failure(s)" % len(fails))
sys.exit(1 if fails else 0)

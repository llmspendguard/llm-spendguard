"""Execution-param PARITY: a (model, reasoning) task fanned via bulk_delegate must reach adapters.call with the
SAME reasoning as the identical SERIAL call — else a fanned judgement (a refuter, a verdict-cached classifier)
silently shifts its verdict distribution. The pinned runner (model_for) rides the metered API, reproducing the
serial call exactly; the LANE fan applies the CLI's OWN reasoning scale (which need not equal the API's), so its
rows are flagged served_by_metered_api=False and carry the requested reasoning for audit. Offline: adapters.call
records what it received — no LLM.
"""
import os
import sys
import tempfile

os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="sg-par-")
os.environ.setdefault("SPENDGUARD_TEST_ISOLATED", "1")
os.environ.setdefault("SPENDGUARD_NO_AUTOINSTALL", "1")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from spendguard import lane_balance, adapters, dispatch   # noqa: E402


def ck(name, cond):
    ok = bool(cond)
    print(("  [OK] " if ok else "  [FAIL] ") + name)
    return [] if ok else [name]


fails = []
dispatch.acquire = lambda *a, **k: 0.0
dispatch.release = lambda *a, **k: None
adapters.provider_for = lambda m: m.split(":", 1)[0]

MODEL = "openai:gpt-5.6-sol"


class _RecordingCall:
    """Stand-in for adapters.call that records each call into self.calls (INSTANCE state, not a module-level
    container) and returns a metered ('api') verdict, so the pinned rows read served_by_metered_api=True."""

    def __init__(self):
        self.calls = []

    def __call__(self, model, prompt, max_tokens=None, system=None, reasoning=None, schema=None, timeout_s=None,
                 sig=None, retries=2, files=None, _no_guard=False, no_metered_fallback=False, images=None,
                 no_substitution=False, metered_only=False):
        self.calls.append({"model": model, "reasoning": reasoning, "metered_only": metered_only})
        prov, raw = model.split(":", 1)
        return {"text": "verdict", "cost": 0.001, "provider": prov, "model": raw, "executor": "api", "effort": reasoning}


_rec = _RecordingCall()
adapters.call = _rec

print("-- the SERIAL call and the FANNED pinned call reach adapters.call with the SAME (model, reasoning) --")
_rec.calls.clear()
adapters.call(MODEL, "refute claim X", reasoning="minimal")           # the serial reference call
_serial = dict(_rec.calls[-1])
_rec.calls.clear()                                                    # isolate the fanned calls from the reference
res = lane_balance.bulk_delegate(["refute A", "refute B", "refute C"], "refute:panel",
                                 model_for=lambda t: MODEL, reasoning="minimal", task_key=lambda t: t,
                                 return_keyed=True, force=True)
_fanned = list(_rec.calls)
fails += ck("the serial call used reasoning='minimal'", _serial["reasoning"] == "minimal")
fails += ck("EVERY fanned call reached adapters.call with the IDENTICAL reasoning (parity, not a different tier)",
            len(_fanned) == 3 and all(c["reasoning"] == "minimal" and c["model"] == MODEL for c in _fanned))

print("\n-- every row RECORDS the requested reasoning + that it ran the faithful metered path (parity is auditable) --")
fails += ck("each row carries reasoning='minimal'", all(r.get("reasoning") == "minimal" for r in res.values()))
fails += ck("each row records the applied effort (from the call)", all(r.get("effort") == "minimal" for r in res.values()))
fails += ck("each pinned row is served_by_metered_api=True (the faithful, serial-equivalent path)",
            all(r.get("served_by_metered_api") for r in res.values()))

print(f"\n{'[FAIL]' if fails else 'OK'} test_bulk_reasoning_parity: {len(fails)} failure(s)")
sys.exit(1 if fails else 0)

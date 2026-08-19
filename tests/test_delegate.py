"""spendguard.lane_balance.delegate — offload one task to the cheapest VIABLE idle lane ($0), least-utilised first,
codex EXCLUDED (its CLI is agent-slow), EMPTY output falls through, and a metered-API answer is flagged billed. So a
Claude Code session can push heavy work onto idle gemini/zai plans and spend only coordination. Offline: adapters.call
+ utilisation + config are stubbed; no LLM, no lane CLI.
"""
import os
import sys
import tempfile

os.environ.setdefault("SPENDGUARD_HOME", tempfile.mkdtemp(prefix="sg-deleg-"))
os.environ.setdefault("SPENDGUARD_TEST_ISOLATED", "1")
os.environ.setdefault("SPENDGUARD_NO_AUTOINSTALL", "1")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from spendguard import lane_balance, adapters, config                                  # noqa: E402


def ck(name, cond):
    ok = bool(cond)
    print(("  [OK] " if ok else "  [FAIL] ") + name)
    return [] if ok else [name]


fails = []
_ocfg, _outil, _ocall = config._cfg_get, lane_balance.lane_utilization, adapters.call


def _cfg(section, key, default=None):
    if section == "advisor" and key == "lane_models":
        return {"gemini": "gemini-3.7-flash-low", "zai-coding": "glm-4.6", "codex": "gpt-5.5"}
    if section == "advisor" and key == "delegate_lanes":
        return None                                       # → default viable set [gemini, zai-coding]
    return _ocfg(section, key, default)


try:
    config._cfg_get = _cfg
    # gemini LESS utilised than zai → gemini is tried first; codex present but must be excluded
    lane_balance.lane_utilization = lambda: {"lanes": [
        {"lane": "gemini", "utilization": 0.1}, {"lane": "zai-coding", "utilization": 0.5},
        {"lane": "codex", "utilization": 0.0}]}

    print("-- picks the least-utilised VIABLE lane; codex is never tried --")
    seen = []

    def _call_ok(model, task, **kw):
        seen.append(model)
        return {"text": f"answer from {model}", "cost": 0.0, "executor": model.split(":")[0], "error": None}
    adapters.call = _call_ok
    r = lane_balance.delegate("do X")
    fails += ck("least-utilised viable lane chosen (gemini < zai)", r["lane"] == "gemini")
    fails += ck("...at its configured LOW model", r["model"] == "gemini:gemini-3.7-flash-low")
    fails += ck("codex is NOT tried (excluded from delegation)", all("gpt-5.5" not in m for m in seen))
    fails += ck("returns the answer text + $0", r["text"].startswith("answer from") and r.get("billed") is False)

    print("\n-- EMPTY output is a failure → fall through to the next lane --")
    seen.clear()

    def _call_empty_gemini(model, task, **kw):
        seen.append(model)
        return {"text": ("" if "gemini" in model else "zai has it"), "cost": 0.0,
                "executor": model.split(":")[0], "error": None}
    adapters.call = _call_empty_gemini
    r2 = lane_balance.delegate("do Y")
    fails += ck("gemini empty → falls through to zai", r2["lane"] == "zai-coding" and r2["text"] == "zai has it")
    fails += ck("...both lanes were tried in order", len(seen) == 2 and "gemini" in seen[0])

    print("\n-- a metered-API answer is flagged billed (never a silent charge) --")

    def _call_billed(model, task, **kw):
        return {"text": "api answered", "cost": 0.0066, "executor": None, "error": None}
    adapters.call = _call_billed
    r3 = lane_balance.delegate("do Z")
    fails += ck("billed=True when cost>0 (metered fallback surfaced)", r3.get("billed") is True)
finally:
    config._cfg_get, lane_balance.lane_utilization, adapters.call = _ocfg, _outil, _ocall

print(f"\n{'[FAIL]' if fails else 'OK'} test_delegate: {len(fails)} failure(s)")
sys.exit(1 if fails else 0)

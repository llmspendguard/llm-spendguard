"""Guard: lane_balance.delegate must resolve a lane's model through the SSOT resolver, NEVER stringify a per-tier
map into the model id.

The regression this locks out cost ~18s per one-shot: `lane_models["codex"]` is a per-tier map
{"cheap": "gpt-5.6-luna", "strong": "gpt-5.6-sol"} (codex/claude-code/gemini are all maps; only zai is a plain
string). delegate used to build the model id as f"{prov}:{lm[lane]}", which stringified the WHOLE dict into
"openai:{'cheap': 'gpt-5.6-luna', ...}" — codex 400s that ("model not supported with a ChatGPT account") and the
on-lane retry/backoff ladder ran ~18s before an eventual clean call. The fix resolves the tier first
(lane_catalog.lane_model_for_tier / configured_base) so the lane is handed a VALID id and rides the warm daemon.

Offline: adapters.call is monkeypatched to CAPTURE the model id (no live call), and the lane_models config is
injected, so the test asserts the dispatched id purely from the resolution logic — no network, no LLM, no plan probe.
"""
import os
import sys
import tempfile

os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="sg-delegate-tier-")
os.environ.setdefault("SPENDGUARD_TEST_ISOLATED", "1")
os.environ.setdefault("SPENDGUARD_NO_AUTOINSTALL", "1")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from spendguard import lane_balance, config, adapters  # noqa: E402


def main():
    fails = []
    captured = {}

    def ck(label, condition, detail=""):
        print(f"  [{'OK' if condition else 'FAIL'}] {label}" + (f" — {detail}" if detail and not condition else ""))
        if not condition:
            fails.append(label)

    # Inject a per-tier lane_models for codex (a MAP) + a plain-string lane, so resolution is exercised deterministically.
    injected = {"codex": {"cheap": "gpt-5.6-luna", "strong": "gpt-5.6-sol"}, "zai-coding": "glm-5.3"}
    real_cfg = config._cfg_get

    def fake_cfg(section, key, default=None):
        if (section, key) == ("advisor", "lane_models"):
            return dict(injected)
        if (section, key) == ("advisor", "lane_bandit"):
            return False
        return real_cfg(section, key, default)

    config._cfg_get = fake_cfg
    lane_balance.config._cfg_get = fake_cfg
    # A codex lane that always has headroom, so delegate's ordering picks it.
    lane_balance.lane_utilization = lambda *a, **k: {"lanes": [{"lane": "codex", "utilization": 0.0},
                                                               {"lane": "zai-coding", "utilization": 0.1}]}

    def capture_call(model, task, **kwargs):
        captured["model"] = model
        captured["reasoning"] = kwargs.get("reasoning")
        return {"text": "OK", "cost": 0.0, "executor": "codex"}

    adapters.call = capture_call
    lane_balance.adapters.call = capture_call

    # ── codex lane at low reasoning → the CHEAP tier model, as a valid "provider:id" (no dict stringification) ──
    captured.clear()
    r = lane_balance.delegate("hi", lanes=["codex"], reasoning="low", intent="t")
    m = captured.get("model")
    ck("codex one-shot dispatched a model id", bool(m), captured)
    ck("the id is NOT a stringified dict (no '{' leaked in)", m is not None and "{" not in m, m)
    ck("low reasoning resolved the CHEAP tier model", m == "openai:gpt-5.6-luna", m)
    ck("delegate returned the lane's answer", isinstance(r, dict) and r.get("text") == "OK", r)

    # ── strong reasoning → the STRONG tier model ──
    captured.clear()
    lane_balance.delegate("hi", lanes=["codex"], reasoning="high", intent="t")
    ck("high reasoning resolved the STRONG tier model", captured.get("model") == "openai:gpt-5.6-sol", captured.get("model"))

    # ── a plain-string lane (zai) resolves to its single id, unchanged ──
    captured.clear()
    lane_balance.delegate("hi", lanes=["zai-coding"], reasoning="low", intent="t")
    mz = captured.get("model")
    ck("plain-string lane dispatches its one id (no '{')", mz is not None and "{" not in mz and mz.endswith(":glm-5.3"), mz)

    # ── an UNCONFIGURED lane (empty lane_models entry → falsy) is filtered pre-loop and NAMED in coverage, not dropped ──
    injected["codex"] = {}
    captured.clear()
    r = lane_balance.delegate("hi", lanes=["codex"], reasoning="low", intent="t")
    ck("unconfigured lane did NOT dispatch a call", "model" not in captured, captured)
    cov = (r or {}).get("coverage") or {}
    ck("unconfigured lane is named in coverage.skipped_unconfigured",
       cov.get("skipped_unconfigured") == ["codex"], cov)

    # ── a CONFIGURED lane (truthy map) whose tier values are all empty resolves to no id → NAMED in
    #    coverage.unresolved_model and NOT dispatched — the loop's record-then-skip, never a silent continue ──
    injected["codex"] = {"cheap": "", "strong": ""}           # truthy map (passes the pre-loop filter) but no usable id
    captured.clear()
    r = lane_balance.delegate("hi", lanes=["codex"], reasoning="low", intent="t")
    ck("unresolvable-model lane did NOT dispatch a call", "model" not in captured, captured)
    cov = (r or {}).get("coverage") or {}
    ck("unresolvable-model lane is named in coverage.unresolved_model", cov.get("unresolved_model") == ["codex"], cov)

    print(f"\n{'[FAIL]' if fails else 'OK'} test_delegate_resolves_lane_tier_model: {len(fails)} failure(s)")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())

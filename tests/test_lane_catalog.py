"""Lane model catalog — the source of truth for what each lane invokes: provider · reasoning quirk · use-names,
priced by BASE + provider. Guards the "proper marking" the feature exists for: the reasoning SUFFIX parse is
lane-aware (only a suffix lane splits, and only a KNOWN level — never a blind strip that could maim another lane's
id), the use-names expand the reasoning axis, the provider comes from `_LANES` (so pricing disambiguates), and cost
resolves the BASE at the lane's provider. Offline: isolated config, stubbed price, no LLM.
"""
import os
import sys
import tempfile

os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="sg-lanecat-")
os.environ.setdefault("SPENDGUARD_TEST_ISOLATED", "1")
os.environ.setdefault("SPENDGUARD_NO_AUTOINSTALL", "1")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from spendguard import lane_catalog as lc, config, pricing                             # noqa: E402


def ck(name, cond):
    ok = bool(cond)
    print(("  [OK] " if ok else "  [FAIL] ") + name)
    return [] if ok else [name]


fails = []
# seed the lane models into the isolated config (what each lane is configured to invoke)
_LANE_MODELS = {"gemini": "gemini-3.7-flash-low", "codex": "gpt-5.5", "zai-coding": "glm-5.3"}
_o_cfg = config._cfg_get


def _fake_cfg(section, key, default=None):
    if section == "advisor" and key == "lane_models":
        return dict(_LANE_MODELS)
    return _o_cfg(section, key, default)


config._cfg_get = _fake_cfg
# stub pricing so cost() is deterministic and independent of the synced table — keyed by the BASE model
_o_price = pricing.realtime_cost
_BASE_RATE = {"gemini-3.7-flash": 4.5, "gpt-5.5": 35.0, "glm-5.3": 5.8}
pricing.realtime_cost = lambda model, in_tok, out_tok=0, provider=None: _BASE_RATE.get(model)

try:
    print("-- parse_use_name is LANE-AWARE: suffix lanes split a KNOWN level, others never do --")
    fails += ck("gemini (suffix): gemini-3.7-flash-low → (gemini-3.7-flash, low)",
                lc.parse_use_name("gemini-3.7-flash-low", "gemini") == ("gemini-3.7-flash", "low"))
    fails += ck("gemini: a bare base is unchanged (gemini-3.7-flash, None)",
                lc.parse_use_name("gemini-3.7-flash", "gemini") == ("gemini-3.7-flash", None))
    fails += ck("codex (param): gpt-5.5 keeps its whole id — effort rides a param, not the name",
                lc.parse_use_name("gpt-5.5", "codex") == ("gpt-5.5", None))
    fails += ck("★ a NON-suffix lane's model ending in '-high' is NEVER mis-split (glm-x-high stays whole)",
                lc.parse_use_name("glm-x-high", "zai-coding") == ("glm-x-high", None))

    print("\n-- provider comes from the lane registry (so pricing can disambiguate a shared model) --")
    fails += ck("gemini → gemini", lc.lane_provider("gemini") == "gemini")
    fails += ck("codex → openai", lc.lane_provider("codex") == "openai")
    fails += ck("zai-coding → zai", lc.lane_provider("zai-coding") == "zai")

    print("\n-- use-names expand the REASONING axis for suffix lanes (the marking that lands in the table) --")
    fails += ck("gemini use-names = base × {low,medium,high}",
                lc.use_names("gemini") == ["gemini-3.7-flash-low", "gemini-3.7-flash-medium", "gemini-3.7-flash-high"])
    fails += ck("codex use-names = just the base (effort out-of-band)", lc.use_names("codex") == ["gpt-5.5"])

    print("\n-- cost() prices the BASE at the lane's provider (effort changes token count, not the rate) --")
    fails += ck("gemini-3.7-flash-high prices at the base gemini-3.7-flash rate ($4.5)",
                lc.use_name_cost("gemini-3.7-flash-high", 1_000_000, 1_000_000, "gemini") == 4.5)
    fails += ck("codex gpt-5.5 prices at $35", lc.use_name_cost("gpt-5.5", 1_000_000, 1_000_000, "codex") == 35.0)

    print("\n-- arms(): the bandit's arm set — each reasoning variant is a DISTINCT arm --")
    a = lc.arms(["gemini", "codex"])
    fails += ck("gemini contributes 3 reasoning arms + codex 1", len(a) == 4)
    fails += ck("...and a gemini -high variant is present as its own arm",
                ("gemini", "gemini-3.7-flash-high") in a)
    fails += ck("...zai-coding excluded when not in the filter", not any(l == "zai-coding" for l, _ in a))
finally:
    config._cfg_get = _o_cfg
    pricing.realtime_cost = _o_price

print(f"\n{'[FAIL]' if fails else 'OK'} test_lane_catalog: {len(fails)} failure(s)")
sys.exit(1 if fails else 0)

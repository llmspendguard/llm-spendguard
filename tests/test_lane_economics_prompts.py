"""Guards for PROMPT-metered lane economics (e.g. the z.ai Coding Plan) and the self-use reserve that protects a
stops-dead prompt budget. These lock the properties that make #2 correct:

  1. a lane is prompt-metered IFF it has a declared prompt budget (structural, no meter-type flag to interpret);
  2. its economics are computed in the NATIVE unit — prompts/window, $/prompt exact, $/token from MEASURED
     tokens/prompt — and consumption is spendguard's own (flagged visible_only);
  3. once spendguard's own consumption reaches the self-use cap, the lane is RESERVED, and the router drops it from
     discretionary routing so the rest of the budget is left for real coding.
"""
from spendguard import lane_economics as le


def _cfg(monkeypatch, budget=None, selfuse=0.25, fee=80.0):
    """Point the config-backed readers at test values without touching the real config file."""
    budgets = {"zai-coding": budget} if budget else {}
    def fake(section, key, default=None):
        if section == "subscription" and key == "lane_prompt_budget":
            return budgets
        if section == "advisor" and key == "prompt_lane_selfuse_cap_frac":
            return selfuse
        return default
    monkeypatch.setattr(le.config, "_cfg_get", fake)


def test_prompt_metered_lane_detected_only_via_budget(monkeypatch):
    _cfg(monkeypatch, budget={"prompts": 2000, "window_days": 7})
    assert le._prompt_metered_lanes() == ["zai-coding"]
    assert le._prompt_budget("zai-coding") == {"prompts": 2000, "window_days": 7.0}
    assert le._prompt_budget("codex") is None            # no budget declared → not prompt-metered


def test_malformed_budget_is_not_prompt_metered(monkeypatch):
    _cfg(monkeypatch, budget={"prompts": 2000})           # missing window_days → rejected
    assert le._prompt_metered_lanes() == []
    assert le._prompt_budget("zai-coding") is None


def test_prompt_economics_native_unit_and_dollar_math(monkeypatch):
    _cfg(monkeypatch, budget={"prompts": 2000, "window_days": 7}, fee=80.0)
    monkeypatch.setattr(le, "_prompts_consumed", lambda lane, wd: 400)     # spendguard sent 400 this window
    monkeypatch.setattr(le, "_tokens_per_prompt", lambda lane: 20000.0)    # measured 20k tok/prompt
    e = le.prompt_economics("zai-coding", 80.0)
    assert e["meter"] == "prompts" and e["visible_only"] is True
    assert e["budget_prompts"] == 2000 and e["remaining_prompts"] == 1600
    # $/prompt = fee * (7/30) / 2000
    assert abs(e["usd_per_prompt"] - (80.0 * 7.0 / 30.0) / 2000) < 1e-9
    # $/token = $/prompt / tokens_per_prompt
    assert abs(e["usd_per_tok"] - e["usd_per_prompt"] / 20000.0) < 1e-15


def test_selfuse_cap_reserves_the_lane(monkeypatch):
    _cfg(monkeypatch, budget={"prompts": 2000, "window_days": 7}, selfuse=0.25)
    monkeypatch.setattr(le, "_tokens_per_prompt", lambda lane: 1000.0)
    # under the cap (0.25 * 2000 = 500): not reserved
    monkeypatch.setattr(le, "_prompts_consumed", lambda lane, wd: 499)
    assert le.prompt_economics("zai-coding", 80.0)["reserved"] is False
    assert le.prompt_lane_reserved("zai-coding") is False
    # at/over the cap: reserved
    monkeypatch.setattr(le, "_prompts_consumed", lambda lane, wd: 500)
    assert le.prompt_economics("zai-coding", 80.0)["reserved"] is True
    assert le.prompt_lane_reserved("zai-coding") is True


def test_reserve_never_applies_to_token_metered_lane(monkeypatch):
    _cfg(monkeypatch, budget={"prompts": 2000, "window_days": 7})
    # a lane with no declared prompt budget is never reserved, regardless of its call volume
    assert le.prompt_lane_reserved("agy") is False
    assert le.prompt_lane_reserved("claude-code") is False


def test_economics_appends_prompt_row(monkeypatch):
    _cfg(monkeypatch, budget={"prompts": 2000, "window_days": 7})
    monkeypatch.setattr(le, "_prompts_consumed", lambda lane, wd: 100)
    monkeypatch.setattr(le, "_tokens_per_prompt", lambda lane: 5000.0)
    monkeypatch.setattr(le, "_plan_total_fee", lambda: (80.0, False))
    # zai-coding present in headroom as a no-token-gauge lane; economics() must still give it a prompt row
    rows = [{"lane": "zai-coding", "provider": "zai", "known": False, "remaining_pct": None, "buckets": []}]
    out = le.economics(headroom_rows=rows, fee_by_lane={"zai-coding": 80.0})
    zai = [e for e in out if e["lane"] == "zai-coding"]
    assert len(zai) == 1 and zai[0]["meter"] == "prompts" and zai[0]["remaining_prompts"] == 1900

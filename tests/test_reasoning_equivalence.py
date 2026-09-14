"""reasoning_equivalence — the canonical lane↔metered map that makes the atomic (lane, metered) pair provable.

PINS (the invariants Ash required):
  · PROVIDER-LOCK — every cell's metered fallback is the SAME provider (a pinned agy/Gemini call never falls to codex).
  · EQUAL-OR-GREATER — no cell ever UNDER-reasons: the metered effort rank is >= the lane's (or both have no param).
  · agy/Gemini 'minimal' → the lane's DEFAULT tier (not a silent floor to 'low'), matching _compose_gemini_reasoning.
  · stale-ALIAS resolution — claude-haiku-4-5 (the CLI alias) → the SERVED dated id claude-haiku-4-5-20251001, so the
    atomic pair never strands its own fallback (metered_fallback_id, $0 from the served-list cache).
  · date-suffix parsing (_is_dated_variant) is a FIXED-FORMAT parse, never a fuzzy match.
  · bake-off PROVEN-LESSER overlay — a recorded cheaper effort overrides the derived cell (learning is maintained).

Offline + hermetic: config.lane_models, the served catalog, served_check and pricing are STUBBED, so the test does
not depend on the live config/catalog and makes no LLM call and no network call.
"""
import os
import sys
import tempfile

os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="sg-re-")
os.environ.setdefault("SPENDGUARD_TEST_ISOLATED", "1")
os.environ.setdefault("SPENDGUARD_NO_AUTOINSTALL", "1")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from spendguard import (reasoning_equivalence as RE, adapters, lane_catalog,   # noqa: E402
                        config, catalog, vendor_call, models, pricing)


def report_check(name, cond):
    """Print one PASS/FAIL line and return [] on pass or [name] on fail, so the caller accumulates failures."""
    print(("  [OK] " if cond else "  [FAIL] ") + name)
    return [] if cond else [name]


fails = []

# ── HERMETIC STUBS — the map's four external reads, pinned to a known fixture so the test is deterministic ──────────
LANE_MODELS = {"claude-code": {"cheap": "claude-haiku-4-5", "strong": "claude-opus-4-8"},
               "codex": {"cheap": "gpt-5.6-luna", "strong": "gpt-5.6-sol"},
               "gemini": {"cheap": "gemini-3.8-flash-low", "strong": "gemini-3.8-flash-high"},
               "zai-coding": "glm-5.3"}
_real_cfg = config._cfg_get


def _cfg(section, key, default=None):
    if section == "advisor" and key == "lane_models":
        return LANE_MODELS
    return _real_cfg(section, key, default)


config._cfg_get = _cfg
lane_catalog.config._cfg_get = _cfg                       # lane_catalog imported config as a name

# the served metered catalog — NOTE: bare 'claude-haiku-4-5' is NOT served, only the DATED id is (the real drift)
SERVED = {"anthropic": ["claude-haiku-4-5-20251001", "claude-opus-4-8"],
          "openai": ["gpt-5.6-sol", "gpt-5.6-luna"],
          "gemini": ["gemini-3.8-flash"],
          "zai": ["glm-5.3"]}
catalog.live_model_ids = lambda v: SERVED.get(v)
catalog.lane_model_ids = lambda v: None


def _served_check(vendor, model):
    ids = SERVED.get(vendor)
    if ids is None:
        return "unchecked"
    return "served" if (model in ids or model.split(":", 1)[-1] in ids) else "stale"


vendor_call.served_check = _served_check
_PRICED = set(sum(SERVED.values(), [])) | {"claude-haiku-4-5"}   # the bare alias is priced too (dated resolves to same $)


def _price_card(model, provider=None):
    """Stub pricing.price — a $0 CARD lookup (no token counts): returns a card for a priced id, raises KeyError
    otherwise (exactly the shape _metered_priced_served relies on to decide priced/unpriced)."""
    if model in _PRICED or model.split(":", 1)[-1] in _PRICED:
        return {"in": 0.001, "out": 0.001, "source": "test"}
    raise KeyError(model)


pricing.price = _price_card
# gpt-5.6-sol is a verified 'none'-floor model (rejects 'minimal') — record the fact so the metered side matches codex
models.add_fact("gpt-5.6-sol", "reasoning", "none", source="test")

print("-- date-suffix parsing is a FIXED format, never fuzzy --")
fails += report_check("claude-haiku-4-5-20251001 IS a dated variant of claude-haiku-4-5",
                      adapters._is_dated_variant("claude-haiku-4-5", "claude-haiku-4-5-20251001"))
fails += report_check("claude-haiku-4-5-mini-20251001 is NOT (an inner segment, not a pure date suffix)",
                      not adapters._is_dated_variant("claude-haiku-4-5", "claude-haiku-4-5-mini-20251001"))
fails += report_check("claude-haiku-4-5-2025 is NOT (wrong digit count)",
                      not adapters._is_dated_variant("claude-haiku-4-5", "claude-haiku-4-5-2025"))

print("\n-- metered_fallback_id resolves the STALE bare alias → the served DATED id (no stranded fallback) --")
_mid, _tier = adapters.metered_fallback_id("anthropic", "claude-haiku-4-5")
fails += report_check("claude-haiku-4-5 → claude-haiku-4-5-20251001",
                      _mid == "claude-haiku-4-5-20251001" and _tier is None)
fails += report_check("an already-served id is unchanged (claude-opus-4-8)",
                      adapters.metered_fallback_id("anthropic", "claude-opus-4-8")[0] == "claude-opus-4-8")

print("\n-- the whole map: PROVIDER-LOCK + EQUAL-OR-GREATER on every cell (never under-reason, never cross-vendor) --")
rows = RE.audit_cells()
fails += report_check("map is non-empty (all 4 lanes derived)", len(rows) >= 16 and {r["lane"] for r in rows} ==
                      {"claude-code", "codex", "gemini", "zai-coding"})
fails += report_check("PROVIDER-LOCK: every cell's provider == its lane's provider",
                      all(r["provider"] == lane_catalog.lane_provider(r["lane"]) for r in rows))


def _is_equal_or_greater(cell):
    """metered effort is EQUAL-OR-GREATER than the lane's (or both have no param) — the core no-under-reason rule."""
    lr, mr = RE._rank(cell["lane_effort"]), RE._rank(cell["metered_effort"])
    if cell["lane_effort"] is None and cell["metered_effort"] is None:
        return True
    if lr is None or mr is None:
        return cell["status"] == "needs_bakeoff"          # incomparable is only OK when flagged for a bake-off
    return mr >= lr


fails += report_check("EQUAL-OR-GREATER: no cell under-reasons (metered effort rank >= lane effort rank)",
                      all(_is_equal_or_greater(RE.resolve_metered(r["lane"], r["model"], r["level"])) for r in rows))
fails += report_check("every cell availability='yes' (equal model priced + confirmed served on the metered API)",
                      all(r["availability"] == "yes" for r in rows))

print("\n-- specific cells reconcile with the real lane behavior --")
sol_min = RE.resolve_metered("codex", "gpt-5.6-sol", "minimal")
fails += report_check("codex/gpt-5.6-sol @ minimal → EQUAL, metered 'none' (both channels floor to none)",
                      sol_min["status"] == "equal" and sol_min["metered_effort"] == "none")
luna_min = RE.resolve_metered("codex", "gpt-5.6-luna", "minimal")
fails += report_check("codex/gpt-5.6-luna @ minimal → metered_greater (lane none < metered minimal; luna 400s on none)",
                      luna_min["status"] == "metered_greater" and luna_min["lane_effort"] == "none"
                      and luna_min["metered_effort"] == "minimal")
gem_min = RE.resolve_metered("gemini", "gemini-3.8-flash", "minimal")
fails += report_check("agy/gemini @ minimal → lane DEFAULT tier (medium), NOT a floor to 'low'; metered rounds UP",
                      gem_min["lane_effort"] == "medium" and gem_min["metered_effort"] == "medium"
                      and gem_min["status"] == "round_up")
gem_low = RE.resolve_metered("gemini", "gemini-3.8-flash", "low")
fails += report_check("agy/gemini @ low → EQUAL (suffix -low ↔ metered reasoning_effort=low)",
                      gem_low["status"] == "equal" and gem_low["metered_effort"] == "low"
                      and gem_low["lane_use_name"] == "gemini-3.8-flash-low")
haiku = RE.resolve_metered("claude-code", "claude-haiku-4-5", "medium")
fails += report_check("claude-code/claude-haiku-4-5 → the served DATED metered id, availability='yes'",
                      haiku["metered_model"] == "claude-haiku-4-5-20251001" and haiku["availability"] == "yes")

print("\n-- PROVEN-LESSER requires an AGENTIC verdict — free-text/unjudged is REFUSED ('equally good' is a MEANING call) --")
_refused = False
try:
    RE.record_equivalence("codex", "gpt-5.6-luna", "minimal", "none", verdict={"note": "one run looked fine"})
except ValueError:
    _refused = True
fails += report_check("an unjudged (no judged_equal/judge_model/sample_n) verdict is REFUSED", _refused)
luna_still = RE.resolve_metered("codex", "gpt-5.6-luna", "minimal")
fails += report_check("...and nothing was recorded — the cell stays derived (metered_greater), not proven_lesser",
                      luna_still["status"] == "metered_greater")

print("\n-- an AFFIRMATIVE agentic verdict IS recorded and overlays the derived cell (learning maintained) --")
RE.record_equivalence("codex", "gpt-5.6-luna", "minimal", "none",
                      verdict={"judged_equal": True, "judge_model": "openai:gpt-5-nano", "sample_n": 40,
                               "good_rate_lesser": 0.95, "good_rate_greater": 0.95, "note": "A/B n=40 equal"})
luna_after = RE.resolve_metered("codex", "gpt-5.6-luna", "minimal")
fails += report_check("after a judged verdict: luna@minimal → proven_lesser, metered 'none' (proven-equal cheaper)",
                      luna_after["status"] == "proven_lesser" and luna_after["metered_effort"] == "none")
fails += report_check("the learning persists with its verdict (load_learnings carries judged_equal)",
                      (RE.load_learnings().get("codex|gpt-5.6-luna|minimal", {}).get("verdict") or {})
                      .get("judged_equal") is True)

print("\n-- the map is BIDIRECTIONAL: resolve_lane (metered→lane) is the inverse of metered_fallback_id (lane→metered) --")
# agy/Gemini is where the names DIFFER (suffix on the lane ↔ bare id + reasoning param on metered) — the round-trip
# must be exact, so a caller who holds EITHER form resolves the other.
_mid, _tier = adapters.metered_fallback_id("gemini", "gemini-3.8-flash-low")
_back = RE.resolve_lane("gemini", _mid, _tier)
fails += report_check("lane→metered→lane round-trips for agy/Gemini (gemini-3.8-flash-low ↔ bare+low)",
                      bool(_back) and _back["lane_use_name"] == "gemini-3.8-flash-low" and _back["lane"] == "gemini")
# openai: the lane use-name IS the metered id → resolves to the codex lane unchanged
_oa = RE.resolve_lane("openai", "gpt-5.6-luna")
fails += report_check("metered→lane for openai → codex lane, same use-name",
                      bool(_oa) and _oa["lane"] == "codex" and _oa["lane_use_name"] == "gpt-5.6-luna")
# a metered-only vendor (no subscription lane) has no lane form → None (never a wrong guess)
fails += report_check("a metered-only provider (no lane) → None", RE.resolve_lane("moonshot", "kimi-k3") is None)

print(f"\n{'[FAIL]' if fails else 'OK'} test_reasoning_equivalence: {len(fails)} failure(s)")
sys.exit(1 if fails else 0)

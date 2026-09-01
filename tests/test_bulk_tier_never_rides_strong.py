"""A --tier bulk fan-out is a COST RAIL: symgrep's one-sentence describe corpus declared `--tier cheap` must be
STRUCTURALLY unable to land on the Opus/claude-code lane. Pins the confinement so a prose fix can't regress it:

  1. bulk_delegate(tier="cheap"), given a lane pool that INCLUDES the Opus lane (claude-code = claude-opus-4-8, a
     strong-tier model), makes ZERO calls to any strong-tier model and never touches the claude-code lane — the fan
     is confined to lanes whose configured base is in the group; and each call PINS the model (no_substitution=True)
     so the bandit can't swap a cheap lane's model for Opus.
  2. FAIL-CLOSED: an undeclared/unserved tier spends NOTHING and returns all-error rows (undescribed, re-runnable) —
     it NEVER widens back to all idle lanes and never falls onto a premium lane.
  3. No tier → the path is unchanged (substitution allowed, all lanes eligible).

Hermetic: config + adapters.call + the bandit/cooling state are monkeypatched, so the ALLOW-LIST is the only gate and
the test is robust to real bandit history. Zero spend, no network."""
import sys

from spendguard import lane_balance, adapters, config, lane_bandit, calls

fails = []
def ck(name, cond):
    print(("  [OK] " if cond else "  [FAIL] ") + name)
    if not cond:
        fails.append(name)

# claude-code serves OPUS (a strong-tier model) and IS in the pool — the exact thing that must never be picked.
LANE_MODELS = {"claude-code": "claude-opus-4-8", "codex": "gpt-5.6-sol",
               "gemini": "gemini-3.7-flash-medium", "zai-coding": "glm-5.3"}
TIERS = {"cheap": ["glm-5.3", "gpt-5.6-luna", "gemini-flash-lite-latest"],
         "strong": ["claude-opus-4-8", "gpt-5.6-sol", "gemini-3.7-flash"]}
STRONG = set(TIERS["strong"])

recorded = []
def _rec(model, prompt, **kw):
    recorded.append({"model": model, "no_sub": kw.get("no_substitution")})
    base = model.split(":", 1)[-1]
    prov = model.split(":", 1)[0] if ":" in model else "?"
    return {"text": "ok", "model": base, "provider": prov, "executor": "zai-coding",
            "cost": 0.0, "error": None, "in_tok": 1, "out_tok": 1}

_orig_cfg = config._cfg_get
def _fake_cfg(*a, **k):
    if a[:2] == ("advisor", "lane_models"):
        return dict(LANE_MODELS)
    if a[:2] == ("advisor", "tiers"):
        return {kk: list(vv) for kk, vv in TIERS.items()}
    if a[:2] == ("advisor", "delegate_lanes"):
        return None                                        # all registered lanes eligible (no external allow-list)
    return _orig_cfg(*a, **k)

_saved = (config._cfg_get, adapters.call, lane_bandit.arm_stats, lane_bandit._arm_cooling,
          adapters._lane_cooling, calls.set_context)
config._cfg_get = _fake_cfg
adapters.call = _rec
lane_bandit.arm_stats = lambda intent: {}                  # all arms untried → optimistic → nothing dropped for winrate
lane_bandit._arm_cooling = lambda *a: False                # neutralize cooling so the ALLOW-LIST is the only gate
adapters._lane_cooling = lambda lane: False
calls.set_context = lambda **kw: None
try:
    # 1. tier=cheap with OPUS in the pool → zero strong, never claude-code, model pinned
    recorded.clear()
    res = lane_balance.bulk_delegate(["a", "b", "c", "d"], "symgrep-index", tier="cheap", force=True)
    called = [r["model"].split(":", 1)[-1] for r in recorded]
    ck("tier=cheap actually dispatched (did not fail-close)", len(recorded) > 0)
    ck("ZERO calls to a STRONG model (never claude-opus-4-8 / gpt-5.6-sol / gemini-3.7-flash)",
       all(m not in STRONG for m in called))
    ck("the OPUS (claude-code) model is NEVER called", "claude-opus-4-8" not in called)
    ck("every task produced a row", len(res) == 4)
    ck("every tier call PINS the model (no_substitution=True) — bandit can't swap to Opus",
       all(r["no_sub"] is True for r in recorded))

    # 2. undeclared tier → FAIL CLOSED: zero spend, all-error rows, never widened
    recorded.clear()
    res2 = lane_balance.bulk_delegate(["x", "y"], "symgrep-index", tier="ghost-tier", force=True)
    ck("undeclared tier → ZERO adapters.call (spends nothing)", len(recorded) == 0)
    ck("undeclared tier → all rows are errors (undescribed, re-runnable)",
       len(res2) == 2 and all(r.get("error") and not r.get("text") for r in res2))
    ck("undeclared tier → never widened onto a lane", all(r.get("lane") is None for r in res2))

    # 3. NO tier → unchanged path (substitution allowed)
    recorded.clear()
    res3 = lane_balance.bulk_delegate(["p"], "symgrep-index", force=True)
    ck("no tier → still dispatches", len(recorded) == 1)
    ck("no tier → model NOT pinned (no_substitution False, unchanged behaviour)", recorded[0]["no_sub"] is False)
finally:
    (config._cfg_get, adapters.call, lane_bandit.arm_stats, lane_bandit._arm_cooling,
     adapters._lane_cooling, calls.set_context) = _saved

print(("\n[OK] " if not fails else "\n[FAIL] ") + "bulk_tier_never_rides_strong: %d failure(s)" % len(fails))
sys.exit(1 if fails else 0)

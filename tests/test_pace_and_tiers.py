"""PACE + TIER value-router: the config-driven engine that uses each subscription plan to ~100% of its window and
never over. Four things must hold, and stay held:

  1. _bucket_pace = elapsed_frac − used_frac: POSITIVE when a plan has spent LESS than the elapsed window (behind →
     budget to burn before reset), NEGATIVE when it has spent MORE (ahead → will run out, shed off it), None when the
     window isn't measured yet. This is the per-plan signal; it must be sign-correct or the router pushes the wrong way.
  2. lane_score adds a behind-pace bonus (weight = advisor.lane_pace_weight) — an under-used plan outranks an equal
     plan that is on/ahead of pace, so fungible work fills the plan that would otherwise waste its allowance.
  3. rank_lanes sheds a PROTECTED plan (subscription.pace[lane].policy='protect') once it is AHEAD of pace — held for
     its own window (e.g. Claude Max weekly, when interactive coding can't run anywhere else) — while an UNprotected
     ahead plan stays available. Protection only sheds when ahead: a protected plan that is BEHIND still absorbs work.
  4. tiers() returns the USER-declared routing groups (advisor.tiers) and nothing else — NO built-in groups, the code
     asserts no model capability; a fungible caller asks for a group NAME the user declared, not a pinned model.

Everything here is pure/logic — no network, no spend. Isolated home, self-re-exec like the rest of the suite."""
import os, sys, tempfile

if not os.environ.get("SPENDGUARD_TEST_ISOLATED"):
    os.environ["SPENDGUARD_TEST_ISOLATED"] = "1"
    os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="spendguard-pace-")
    os.execv(sys.executable, [sys.executable] + sys.argv)

from spendguard import route_utility as ru, lane_economics as le, config

fails = []
def ck(name, cond):
    print(("  [OK] " if cond else "  [FAIL] ") + name)
    if not cond:
        fails.append(name)

NOW = 1_000_000_000.0
DAY = 86400.0

# ── 1. _bucket_pace sign + magnitude ────────────────────────────────────────────────────────────────────────────
# A 7-day window, reset 3.5 days out → exactly half elapsed. remaining_pct is what's LEFT.
def _bucket(pct, days_left, period_days=7):
    return {"period_days": period_days, "reset_ts": NOW + days_left * DAY, "remaining_pct": pct}

on_pace   = le._bucket_pace(_bucket(50, 3.5), NOW)   # half elapsed, half left → ~0
behind    = le._bucket_pace(_bucket(90, 3.5), NOW)   # half elapsed, 90% left (used 10%) → +0.4
ahead     = le._bucket_pace(_bucket(10, 3.5), NOW)   # half elapsed, 10% left (used 90%) → -0.4
unknown   = le._bucket_pace({"period_days": None, "reset_ts": None, "remaining_pct": 50}, NOW)
ck("_bucket_pace ~0 on pace (half window, half left)", abs(on_pace) < 0.02)
ck("_bucket_pace POSITIVE when behind (under-used → burn it)", behind is not None and behind > 0.3)
ck("_bucket_pace NEGATIVE when ahead (over-used → shed it)", ahead is not None and ahead < -0.3)
ck("_bucket_pace None when window not measured (no false signal)", unknown is None)

# ── 2. lane_score pace bonus (behind outranks on/ahead) ─────────────────────────────────────────────────────────
row = {"known": True, "remaining_pct": 80, "remaining_abs": None, "reset_ts": NOW + 3 * DAY}
s_none   = ru.lane_score(row, now=NOW, pace=None)
s_behind = ru.lane_score(row, now=NOW, pace=0.4)
s_ahead  = ru.lane_score(row, now=NOW, pace=-0.9)
ck("lane_score behind-pace > no-pace (fills the under-used plan)", s_behind > s_none)
ck("lane_score ahead-pace == baseline (no penalty below 1.0 floor)", abs(s_ahead - s_none) < 1e-9)
ck("lane_score still >= 1.0 (a plan token always beats metered)", s_ahead >= 1.0)

# ── 3. rank_lanes: protect sheds an AHEAD plan, keeps a BEHIND one; unprotected ahead stays available ───────────
rows = [
    {"lane": "claude-code", "provider": "anthropic", "known": True, "remaining_pct": 80, "remaining_abs": None,
     "reset_ts": NOW + 3 * DAY, "buckets": []},
    {"lane": "codex", "provider": "openai", "known": True, "remaining_pct": 80, "remaining_abs": None,
     "reset_ts": NOW + 3 * DAY, "buckets": []},
]
prot = lambda ln: ln == "claude-code"
ranked = ru.rank_lanes(rows, cooling=lambda ln: False, now=NOW,
                       pace_by={"claude-code": -0.4, "codex": 0.4}, protect=prot)
by = {r["lane"]: r for r in ranked}
ck("protected + ahead of pace → shed (available=False)", by["claude-code"]["available"] is False)
ck("...with an explaining reason (answerable 'why not this lane')", "ahead of pace" in by["claude-code"]["why"])
ck("unprotected + behind pace → available and boosted", by["codex"]["available"] is True and by["codex"]["score"] > 1.0)
ck("behind (codex) ranks ahead of shed (claude) in the order", ranked[0]["lane"] == "codex")

# protected but BEHIND must NOT shed (protection preserves the window; it doesn't starve a plan with budget to spend)
ranked_behind = ru.rank_lanes(rows, cooling=lambda ln: False, now=NOW,
                              pace_by={"claude-code": 0.4, "codex": -0.4}, protect=prot)
by_b = {r["lane"]: r for r in ranked_behind}
ck("protected + BEHIND pace → still available (only sheds when ahead)", by_b["claude-code"]["available"] is True)

# No config in the isolated home → tiers() has NO opinion (no built-in groups; capability is never code-asserted).
ck("tiers: no config → {} (ships no built-in groups)", ru.tiers() == {})
ck("tier_models: undeclared group with no config → []", ru.tier_models("strong") == [])

# ── 3b. _protect_policy reads config subscription.pace agentically-free (policy string, general to any lane) ─────
_orig_get = config._cfg_get
def _fake_get(*a, **k):
    if a[:2] == ("subscription", "pace"):
        return {"claude-code": {"policy": "protect"}, "codex": {"policy": "maximize"}}
    if a[:2] == ("advisor", "tiers"):
        return {"cheap": ["my-cheap-model"], "custom": ["x", "y"]}
    return _orig_get(*a, **k)
config._cfg_get = _fake_get
try:
    p = ru._protect_policy()
    ck("_protect_policy: 'protect' → True", p("claude-code") is True)
    ck("_protect_policy: 'maximize' → False", p("codex") is False)
    ck("_protect_policy: unlisted lane → False (default maximize)", p("gemini") is False)
    # ── 4. tiers() = EXACTLY the user's declared groups (no built-in defaults merged in) ─────────────────────────
    t = ru.tiers()
    ck("tiers: returns exactly the user's declared groups", t == {"cheap": ["my-cheap-model"], "custom": ["x", "y"]})
    ck("tier_models returns the list for a declared group", ru.tier_models("custom") == ["x", "y"])
    ck("tier_models: undeclared group → [] (not a crash)", ru.tier_models("nope") == [])
finally:
    config._cfg_get = _orig_get

# ── 5. pace_by_lane smoke: empty in → empty out, exercises the time import with no crash ─────────────────────────
ck("pace_by_lane([]) == {} (no economics rows → no pace, no crash)", le.pace_by_lane([], now=NOW) == {})

print(("\n[OK] " if not fails else "\n[FAIL] ") + "pace_and_tiers: %d failure(s)" % len(fails))
sys.exit(1 if fails else 0)

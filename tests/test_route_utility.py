"""Routing UTILITY score — one comparable score per target so the router drains free plans first (by real headroom,
use-it-or-lose-it), then the cheapest metered that can still pay. Pins:
  (a) _urgency — 1.0 far from reset, rising to the max as it nears, max past it, 1.0 when unknown;
  (b) lane_score — 1.0 + remaining_frac × urgency (always >= 1.0, free); UNKNOWN headroom → None (proxy decides);
  (c) rank_lanes — best headroom first, cooling lanes EXCLUDED, unknown-headroom lanes last;
  (d) rank_metered — cheaper → higher (all < 1, below any lane); a sunk pool that can't cover the call EXCLUDED, an
      on_demand/payg account never gated on balance;
  (e) rank_targets — every free lane (>=1) outranks every paid metered target (<1).
Offline: pricing / balances / cooling stubbed; no network, no LLM.
"""
import os
import sys
import tempfile

os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="sg-routil-")
os.environ.setdefault("SPENDGUARD_TEST_ISOLATED", "1")
os.environ.setdefault("SPENDGUARD_NO_AUTOINSTALL", "1")
# This legacy scorer test isolates headroom/pace ordering. The dedicated quota-preflight acceptance script covers
# the default-on Anthropic avoidance policy and its authorization behavior.
os.environ["SPENDGUARD_AVOID_CLAUDE_LANE"] = "off"
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from spendguard import route_utility as ru, pricing, balances                           # noqa: E402

_base_cfg_get = ru.config._cfg_get
ru.config._cfg_get = lambda section, key, default=None: (
    "" if (section, key) == ("advisor", "shed_lanes") else _base_cfg_get(section, key, default)
)
fails = []


def ck(name, cond):
    ok = bool(cond)
    print(("  [OK] " if ok else "  [FAIL] ") + name)
    if not ok:
        fails.append(name)


now = 1_000_000.0

print("-- (a) _urgency: 1.0 far, → max near, max past, 1.0 unknown --")
ck("far reset (2 days, horizon 1 day) → 1.0", abs(ru._urgency(now + 2 * 86400, now) - 1.0) < 1e-6)
ck("reset already passed → the max (3.0)", abs(ru._urgency(now - 10, now) - 3.0) < 1e-6)
ck("reset in 1h → boosted well above 1", ru._urgency(now + 3600, now) > 2.5)
ck("unknown reset → 1.0 (no invented urgency)", ru._urgency(None, now) == 1.0)

print("\n-- (b) lane_score: 1.0 + frac × urgency; unknown → None --")
ck("90% headroom, far reset → 1.9", abs(ru.lane_score({"known": True, "remaining_pct": 90, "reset_ts": now + 5 * 86400}, now) - 1.9) < 1e-6)
ck("0% headroom, far reset → 1.0 (still free, just no room)", abs(ru.lane_score({"known": True, "remaining_pct": 0, "reset_ts": now + 5 * 86400}, now) - 1.0) < 1e-6)
ck("unknown headroom → None", ru.lane_score({"known": False, "remaining_pct": None}, now) is None)
ck("a soon reset SCORES ABOVE a far reset at equal headroom (use-it-or-lose-it)",
   ru.lane_score({"known": True, "remaining_pct": 50, "reset_ts": now + 3600}, now) >
   ru.lane_score({"known": True, "remaining_pct": 50, "reset_ts": now + 5 * 86400}, now))

print("\n-- (c) rank_lanes: best headroom first, cooling excluded, unknown last --")
rows = [{"lane": "claude-code", "provider": "anthropic", "known": True, "remaining_pct": 84, "reset_ts": now + 5 * 86400},
        {"lane": "codex", "provider": "openai", "known": True, "remaining_pct": 100, "reset_ts": now + 5 * 86400},
        {"lane": "gemini", "provider": "google", "known": True, "remaining_pct": 0, "reset_ts": now + 5 * 86400},
        {"lane": "zai-coding", "provider": "zai", "known": False, "remaining_pct": None, "reset_ts": None}]
ranked = ru.rank_lanes(rows, cooling=lambda ln: ln == "gemini", now=now, pace_by={}, protect=lambda lane: False)
avail = [d["lane"] for d in ranked if d["available"]]
ck("cooling gemini is SURFACED but not available (not silently dropped)",
   any(d["lane"] == "gemini" and d["available"] is False for d in ranked) and "gemini" not in avail)
ck("codex (100%) ranks above claude-code (84%) among available", avail.index("codex") < avail.index("claude-code"))
ck("unknown-headroom zai-coding sorts LAST among available (proxy decides)", avail[-1] == "zai-coding")
ck("...and its score is None", next(d for d in ranked if d["lane"] == "zai-coding")["score"] is None)

print("\n-- (c2) rank_lanes HONOURS advisor.shed_lanes (the bandit must not route ONTO a shed lane) --")
# A lane the operator marked to shed must be held out of discretionary routing REGARDLESS of pace — before this the
# bandit ranked a behind-pace shed lane #1, defeating the shed for every fungible call (route_utility SSOT fix).
from spendguard import config as _cfg                                                    # noqa: E402
_o_shed = _cfg.shed_lanes
try:
    _cfg.shed_lanes = lambda: {"claude-code"}
    # claude-code has the BEST headroom (would rank #1) but is shed; codex/gemini are not.
    shed_rows = [{"lane": "claude-code", "provider": "anthropic", "known": True, "remaining_pct": 100, "reset_ts": now + 5 * 86400},
                 {"lane": "codex", "provider": "openai", "known": True, "remaining_pct": 40, "reset_ts": now + 5 * 86400},
                 {"lane": "gemini", "provider": "google", "known": True, "remaining_pct": 20, "reset_ts": now + 5 * 86400}]
    rk = ru.rank_lanes(shed_rows, cooling=lambda ln: False, now=now)
    av = [d["lane"] for d in rk if d["available"]]
    ck("shed claude-code is HELD OUT despite best headroom (available=False, shed reason)",
       any(d["lane"] == "claude-code" and d["available"] is False and "shed (advisor.shed_lanes)" in d["why"] for d in rk))
    ck("the internal _shed_held bookkeeping key does NOT leak into the returned entries",
       all("_shed_held" not in d for d in rk))
    ck("fungible work routes to the OTHER free lanes (codex, gemini) instead", set(av) == {"codex", "gemini"})
    ck("shed lane keeps its real score (re-admittable, not a dead target)",
       next(d for d in rk if d["lane"] == "claude-code")["score"] is not None)
    # NEVER-WEDGE: if the shed lane is the ONLY lane, it is re-admitted (a preference, not a cap) — never force a
    # fungible call to paid metered when a free lane could serve.
    only = ru.rank_lanes([shed_rows[0]], cooling=lambda ln: False, now=now)
    ck("shed lane re-admitted when it is the only option (never-wedge)", only[0]["available"] is True)
finally:
    _cfg.shed_lanes = _o_shed

# the SSOT PARSER itself (shared by plan_admission + the bandit): comma split, whitespace-trimmed, empties dropped.
_o_cfgget = _cfg._cfg_get
try:
    _cfg._cfg_get = lambda sec, key, default=None: " claude-code , codex ,, " if (sec, key) == ("advisor", "shed_lanes") else default
    ck("config.shed_lanes parses a comma list, trims whitespace, drops empties", _cfg.shed_lanes() == {"claude-code", "codex"})
    _cfg._cfg_get = lambda sec, key, default=None: default
    ck("config.shed_lanes unset → empty set (shed only when actually capped)", _cfg.shed_lanes() == set())
finally:
    _cfg._cfg_get = _o_cfgget

print("\n-- (d) rank_metered: cheaper → higher (<1); sunk-pool-can't-pay excluded; on_demand never gated --")
_costs = {"deepseek:deepseek-v4": 0.002, "moonshot:kimi-k2": 0.010, "openai:gpt-5-nano": 0.001, "gemini:g-flash": 0.05}
_o_cost, _o_bal, _o_decl = pricing.realtime_cost, balances.vendor_balance, balances._declared
try:
    pricing.realtime_cost = lambda spec, i, o: _costs.get(spec)
    # deepseek sunk pool BROKE ($0), moonshot sunk with plenty, openai on_demand, gemini payg
    _bals = {"deepseek": {"kind": "sunk_pool", "available": 0.0, "auto_topup": False},
             "moonshot": {"kind": "sunk_pool", "available": 50.0, "auto_topup": False},
             "openai": {"kind": "on_demand", "available": 10.0, "auto_topup": True},
             "gemini": {"kind": "sunk_pool", "available": 0.0, "auto_topup": False}}
    balances.vendor_balance = lambda p: _bals.get(p, {"kind": "unknown", "available": None})
    balances._declared = lambda p: {"payg": True} if p == "gemini" else {}
    m = ru.rank_metered(list(_costs), 100, 50)
    avail_mt = [d["target"] for d in m if d["available"]]
    ck("deepseek SURFACED but unavailable (sunk pool at $0 can't cover the call)",
       any(d["target"] == "deepseek:deepseek-v4" and d["available"] is False for d in m) and "deepseek:deepseek-v4" not in avail_mt)
    ck("gemini available despite $0 balance (payg reloads)", any(d["target"] == "gemini:g-flash" and d["available"] for d in m))
    ck("cheapest affordable (openai $0.001) ranks first among available metered", avail_mt[0] == "openai:gpt-5-nano")
    ck("every AVAILABLE metered score is below 1.0 (paid < any free lane)", all(d["score"] < 1.0 for d in m if d["available"]))
finally:
    pricing.realtime_cost, balances.vendor_balance, balances._declared = _o_cost, _o_bal, _o_decl

print("\n-- (e) rank_targets: all free lanes (>=1) before all paid metered (<1) --")
_o_cost2, _o_bal2, _o_decl2 = pricing.realtime_cost, balances.vendor_balance, balances._declared
try:
    pricing.realtime_cost = lambda spec, i, o: 0.005
    balances.vendor_balance = lambda p: {"kind": "on_demand", "available": 100.0, "auto_topup": True}
    balances._declared = lambda p: {}
    tg = ru.rank_targets(rows, ["openai:gpt-5-nano"], 100, 50, cooling=lambda ln: False, now=now)
    kinds = [t["kind"] for t in tg if t["available"]]        # among AVAILABLE targets, free lanes precede paid metered
    first_metered = kinds.index("metered") if "metered" in kinds else len(kinds)
    ck("no available lane appears after an available metered (free always outranks paid)", "lane" not in kinds[first_metered:])
    scored_lanes = [t["score"] for t in tg if t["kind"] == "lane" and t["available"] and t["score"] is not None]
    metered_scores = [t["score"] for t in tg if t["kind"] == "metered" and t["available"]]
    ck("every scored lane >= 1.0 and every available metered < 1.0", all(s >= 1.0 for s in scored_lanes) and all(s < 1.0 for s in metered_scores))
finally:
    pricing.realtime_cost, balances.vendor_balance, balances._declared = _o_cost2, _o_bal2, _o_decl2

print(f"\n{'[FAIL]' if fails else 'OK'} test_route_utility: {len(fails)} failure(s)")
sys.exit(1 if fails else 0)

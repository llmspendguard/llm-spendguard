"""Learned cross-lane ROUTER — a DECAYING contextual bandit. The "everyone gets equal use, then we learn what's best
for what, and relearn as models change" brain.

  CONTEXT = the intent (task type).  ARMS = the (lane, use-name) entries from lane_catalog.
  REWARD  = an agentic judge of which lane's output was better (bake-offs, wired SEPARATELY), tilted toward filling
            IDLE plans and cheaper cost.

NON-STATIONARY on purpose (Ash: "after some time or new models we relearn"):
  • evidence DECAYS — exponential forgetting per trial, so a lane that improved isn't buried under old losses;
  • exploration NEVER stops — an ε-floor keeps every live arm in occasional rotation;
  • a brand-new arm (new model / new reasoning level) starts UNTRIED, so it is explored first — equal-start, then
    learn. No explicit "reset" is needed: `choose_arm` only ranks the CURRENT catalog arms, a new one is untried, an
    old one drops out of the catalog and simply stops being offered.

This module is PURE STATE (no LLM): choose / record / score / decay, persisted in the `lane_bandit` table so we
never re-pay to re-learn. The agentic BAKE-OFF judge and the routing wiring build on top — separately, gated and
estimate-first — because those spend.
"""
import contextlib
import datetime
import random
import sqlite3

from . import config

_rng = random.Random()          # module-level so tests can seed it deterministically
DECAY_DEFAULT = 0.95            # exponential forgetting per trial — recent results weigh more (relearn)
EPSILON_DEFAULT = 0.15         # exploration floor — never fully abandon a live arm


def _bcfg(name, default):
    """A float advisor.* knob, defaulted — every bandit parameter is CONFIG, never a hardcoded magic number."""
    try:
        return float(config._cfg_get("advisor", name, None) or default)
    except (TypeError, ValueError):
        return default


def _bandit_db():
    c = sqlite3.connect(config.db_path(), timeout=15)
    c.execute("""CREATE TABLE IF NOT EXISTS lane_bandit(
        intent TEXT, lane TEXT, use_name TEXT,
        trials REAL DEFAULT 0, wins REAL DEFAULT 0, last_ts TEXT,
        PRIMARY KEY (intent, lane, use_name))""")
    return c


def record_trial(intent, lane, use_name, won, ts=None):
    """One trial outcome for (intent, arm). `won` ∈ [0,1] (1 = this arm won the bake-off / was kept; 0 = lost or
    failed; 0.5 = tie). Exponential-forgetting update — trials←trials·γ+1, wins←wins·γ+won — so the win-RATE
    (wins/trials) is a RECENCY-weighted estimate that relearns as models drift. Never raises."""
    if not intent or not lane:
        return
    try:
        g = _bcfg("bandit_decay", DECAY_DEFAULT)
        w = max(0.0, min(1.0, float(won)))
        ts = ts or datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")
        with contextlib.closing(_bandit_db()) as c:
            row = c.execute("SELECT trials, wins FROM lane_bandit WHERE intent=? AND lane=? AND use_name=?",
                            (intent, lane, use_name)).fetchone()
            t0, w0 = (row or (0.0, 0.0))
            c.execute("INSERT OR REPLACE INTO lane_bandit(intent,lane,use_name,trials,wins,last_ts) "
                      "VALUES(?,?,?,?,?,?)", (intent, lane, use_name, t0 * g + 1.0, w0 * g + w, ts))
            c.commit()
    except Exception:
        pass


def arm_stats(intent):
    """{(lane,use_name): {trials, wins, winrate, last_ts}} for an intent (decayed). {} on any error."""
    out = {}
    try:
        with contextlib.closing(_bandit_db()) as c:
            for lane, un, t, w, ts in c.execute(
                    "SELECT lane, use_name, trials, wins, last_ts FROM lane_bandit WHERE intent=?", (intent,)):
                out[(lane, un)] = {"trials": t, "wins": w, "winrate": (w / t if t > 1e-9 else 0.0), "last_ts": ts}
    except Exception:
        pass
    return out


def _arm_cooling(lane, use_name):
    """True if this arm should be skipped right now — its lane, or this (lane, base-model), is on the failure
    cooldown the adapter learned. Reuses the SAME backoff the metered path uses, so the bandit never routes onto a
    known-bad arm."""
    try:
        from . import adapters, lane_catalog
        if adapters._lane_cooling(lane):
            return True
        base, _lv = lane_catalog.parse_use_name(use_name, lane)
        return adapters._lane_model_cooling(lane, base)
    except Exception:
        return False


def _idle_bonus(lane):
    """A gentle tilt toward IDLE plans (fill spare paid capacity), from lane_utilization. 1.0 when unknown/neutral;
    bounded [0.5, 1.5] so it only breaks near-ties, never overrides a real quality gap."""
    try:
        from . import lane_balance
        u = {l["lane"]: l for l in lane_balance.lane_utilization()["lanes"]}.get(lane) or {}
        util = u.get("utilization")
        if util is None:
            return 1.0
        return max(0.5, min(1.5, 1.0 / (0.5 + float(util))))     # idle (low util) → >1; hot → <1
    except Exception:
        return 1.0


def _cost_bonus(lane, use_name):
    """A gentle tilt toward CHEAPER arms (lower API-equivalent rate), from the catalog. 1.0 when unpriced; bounded
    [0.5, 1.5] so cost is a tie-breaker, not the driver (quality leads)."""
    try:
        from . import lane_catalog
        c = lane_catalog.use_name_cost(use_name, 1_000_000, 1_000_000, lane)
        if not c:
            return 1.0
        return max(0.5, min(1.5, 10.0 / (5.0 + float(c))))
    except Exception:
        return 1.0


def arm_score(intent, arm):
    """Exploit ranking for an arm: decayed WIN-RATE × idle-fill × cost tilt. Quality leads (it is what the judge
    learned); idle-fill and cost only break near-ties. Pure read; no LLM."""
    lane, use_name = arm
    wr = arm_stats(intent).get(arm, {}).get("winrate", 0.0)
    return wr * _idle_bonus(lane) * _cost_bonus(lane, use_name)


def choose_arm(intent, arms):
    """Pick ONE arm for this intent. EQUAL-START: any untried arm is explored first (least-recently-tried). Else
    ε-EXPLORE a random live arm (the floor that keeps relearning). Else EXPLOIT the best score. Never returns a
    cooling arm; None if every arm is cooling or `arms` is empty. arms = [(lane, use_name), …] from lane_catalog."""
    live = [a for a in (arms or []) if not _arm_cooling(*a)]
    if not live:
        return None
    st = arm_stats(intent)
    untried = [a for a in live if st.get(a, {}).get("trials", 0.0) < 1.0]
    if untried:
        untried.sort(key=lambda a: st.get(a, {}).get("last_ts") or "")   # never-tried (no ts) first → equal exposure
        return untried[0]
    if _rng.random() < _bcfg("bandit_epsilon", EPSILON_DEFAULT):
        return _rng.choice(live)
    return max(live, key=lambda a: arm_score(intent, a))


def should_bakeoff(intent, arms):
    """Run a 2-arm bake-off this call? YES while an intent is still cold (fewer than `warmup` total trials) so it
    learns fast; afterwards only at rate ε_bakeoff. Needs ≥2 live arms. This is only the PACING decision (pure, no
    spend) — running both arms + judging them is wired separately, gated + estimate-first."""
    live = [a for a in (arms or []) if not _arm_cooling(*a)]
    if len(live) < 2:
        return False
    total = sum(s["trials"] for s in arm_stats(intent).values())
    if total < _bcfg("bandit_bakeoff_warmup", 2.0 * len(live)):
        return True
    return _rng.random() < _bcfg("bandit_bakeoff_rate", 0.10)


def learned_table(intent=None):
    """What the bandit has learned, for `spendguard lanes --learn`: {intent: [(lane, use_name, winrate, trials), …]}
    sorted best-first. All intents when intent is None."""
    out = {}
    try:
        with contextlib.closing(_bandit_db()) as c:
            q = "SELECT intent, lane, use_name, trials, wins FROM lane_bandit"
            rows = c.execute(q + (" WHERE intent=?" if intent else ""), (intent,) if intent else ()).fetchall()
        for it, lane, un, t, w in rows:
            out.setdefault(it, []).append((lane, un, (w / t if t > 1e-9 else 0.0), t))
        for it in out:
            out[it].sort(key=lambda r: -r[2])
    except Exception:
        pass
    return out


def main(argv=None):
    """`spendguard lanes --learn` — print what the bandit has learned per intent (which lane/reasoning wins, and how
    many decayed trials back it)."""
    tbl = learned_table()
    if not tbl:
        print("lane bandit: nothing learned yet — no bake-offs recorded. (Explore/bake-off wiring pending.)")
        return 0
    print("Lane bandit — learned winner per intent (decayed win-rate × trials):")
    for it, rows in sorted(tbl.items()):
        print(f"  {it}")
        for lane, un, wr, t in rows:
            print(f"     {lane:<12} {un:<26} winrate {wr:5.2f}  ({t:.1f} trials)")
    return 0


if __name__ == "__main__":      # python -m spendguard.lane_bandit
    raise SystemExit(main())

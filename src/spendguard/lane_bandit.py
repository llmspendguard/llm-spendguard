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


# ── the agentic REWARD (bake-off judge) and the runners that spend ────────────────────────────────────────────
_JUDGE_OUT_CAP = 200            # A/B/TIE + one short reason — a tiny output; the judge is the ONLY per-bake-off cost


def _bandit_judge_model():
    """The cheap model that judges a bake-off — advisor.bandit_judge_model, else the shared advisor judge (haiku)."""
    return config._cfg_get("advisor", "bandit_judge_model", None) or config.advisor_judge_model()


def bakeoff_judge(task, out_a, out_b, arm_a, arm_b):
    """AGENTIC 2-way judge: which lane's output better accomplishes the task? Returns (winner_arm or None, reason).
    The DECISION is the LLM's (meaning) — only the fixed A/B/TIE token is parsed. Caged under the meta intent
    (attributed, never recurses into the bandit); an EMPTY output loses by default, so no judge call is spent when a
    side is blank (or when the two are identical)."""
    a, b = (out_a or "").strip(), (out_b or "").strip()
    if a and not b:
        return arm_a, "B empty (no judge spend)"
    if b and not a:
        return arm_b, "A empty (no judge spend)"
    if not a and not b:
        return None, "both empty"
    if a == b:
        return None, "identical outputs (no judge spend)"
    prompt = ("Two assistants answered the SAME task. Which answer is better — more correct, complete, and on-format "
              "for the task? Reply with ONLY ONE WORD, nothing else: A, or B, or TIE. (A verbose reply gets truncated "
              "and the call is wasted — the single word is all that is read.)\n\n"
              f"TASK:\n{task[:3000]}\n\n=== ANSWER A ===\n{a[:4000]}\n\n=== ANSWER B ===\n{b[:4000]}\n")
    try:
        from . import adapters, calls
        from .advisor import META                                   # ONE source of the meta-intent prefix
        with calls.context(intent=f"{META}:bandit-judge"):          # caged: attributed, never bandit-routed
            r = adapters.call(_bandit_judge_model(), prompt, max_tokens=_JUDGE_OUT_CAP)
        txt = (r.get("text") or "").strip()
    except Exception:
        return None, None                              # judge CALL failed → NO verdict (reason None): the caller must
    if not txt:                                        # NOT record a false tie about the arms. Empty/truncated = same.
        return None, None
    tok = (txt.split() or [""])[0].upper().strip(".:,)")            # PARSE a known-shape token — not a meaning call
    if tok.startswith("A"):
        return arm_a, "A"
    if tok.startswith("B"):
        return arm_b, "B"
    return None, "tie"                                 # an explicit, DECIDED tie (reason set → recorded 0.5/0.5)


def _run_arm(arm, task, system, reasoning, timeout_s):
    """Run ONE arm on its lane DIRECTLY (no API fallback — a lane that fails or returns blank just LOSES the bake-off,
    so the judge scores the LANE's own output, never a metered stand-in). Records the successful lane call for
    est-value. Returns the text ('' on any failure)."""
    lane, use_name = arm
    try:
        from . import adapters, lane_catalog, calls
        import importlib
        prov = lane_catalog.lane_provider(lane)
        entry = adapters._LANES.get(prov)
        if not entry:
            return ""
        mod = importlib.import_module("." + entry[1], "spendguard")
        base, level = lane_catalog.parse_use_name(use_name, lane)
        # gemini carries effort in the model SUFFIX (pass the whole use-name); the others take it as `reasoning`
        model = use_name if lane_catalog.quirk(lane)["style"] == "suffix" else base
        cap = int(getattr(mod, "TIMEOUT_S", 300))
        r = mod.run_prompt(task, system=system, model=model, timeout=min(cap, int(timeout_s or cap)),
                           reasoning=(level or reasoning))
        if not isinstance(r, dict) or r.get("error") or not (r.get("text") or "").strip():
            return ""
        try:
            calls.record_call(prov, model, "subscription", 0.0, in_tok=r.get("in_tok", 0), out_tok=r.get("out_tok", 0),
                         latency=r.get("latency"), executor=lane)
        except Exception:
            pass
        return r.get("text") or ""
    except Exception:
        return ""


def run_bakeoff(intent, task, system=None, reasoning=None, timeout_s=None):
    """Run the two LEAST-tried live arms on the SAME task, judge which is better, record the outcome for BOTH, and
    RETURN the winner's text (the caller still gets a usable answer). None if fewer than 2 live arms. This is where
    the bandit LEARNS — two $0 lane calls + one cheap judge call."""
    from . import lane_catalog
    dl = config._cfg_get("advisor", "delegate_lanes", None)
    arms = lane_catalog.arms(dl) if dl else lane_catalog.arms()
    live = [a for a in arms if not _arm_cooling(*a)]
    if len(live) < 2:
        return None
    st = arm_stats(intent)
    live.sort(key=lambda a: (st.get(a, {}).get("trials", 0.0), st.get(a, {}).get("last_ts") or ""))
    arm_a, arm_b = live[0], live[1]
    out_a = _run_arm(arm_a, task, system, reasoning, timeout_s)
    out_b = _run_arm(arm_b, task, system, reasoning, timeout_s)
    winner, reason = bakeoff_judge(task, out_a, out_b, arm_a, arm_b)
    if winner == arm_a:
        record_trial(intent, arm_a[0], arm_a[1], 1.0)
        record_trial(intent, arm_b[0], arm_b[1], 0.0)
        return {"text": out_a, "lane": arm_a[0], "use_name": arm_a[1], "why": "bake-off winner"}
    if winner == arm_b:
        record_trial(intent, arm_b[0], arm_b[1], 1.0)
        record_trial(intent, arm_a[0], arm_a[1], 0.0)
        return {"text": out_b, "lane": arm_b[0], "use_name": arm_b[1], "why": "bake-off winner"}
    # winner is None. A DECIDED tie (reason set) records 0.5/0.5; a JUDGE FAILURE (reason None — the judge call
    # errored or its reply was empty/truncated) records NOTHING: a broken judge is not evidence about the arms, and
    # logging it as a tie corrupts the learned table. Still return a usable answer either way.
    _w = arm_a if out_a else arm_b
    if reason is not None:
        record_trial(intent, arm_a[0], arm_a[1], 0.5)
        record_trial(intent, arm_b[0], arm_b[1], 0.5)
    return {"text": out_a or out_b, "lane": _w[0], "use_name": _w[1],
            "why": "bake-off tie" if reason is not None else "judge unavailable (not recorded)"}


def bandit_call(intent, task, system=None, reasoning=None, timeout_s=None):
    """The bandit's ENTRY for a delegatable task: EXPLORE via a bake-off (while cold, or at rate ε) else EXPLOIT the
    learned-winner arm on its lane. Returns {text, lane, use_name, why} of the answering lane, or None if no arm
    could serve it (caller falls back to its normal path). Records outcomes so it keeps learning."""
    from . import lane_catalog
    dl = config._cfg_get("advisor", "delegate_lanes", None)
    arms = lane_catalog.arms(dl) if dl else lane_catalog.arms()
    if should_bakeoff(intent, arms):
        res = run_bakeoff(intent, task, system, reasoning, timeout_s)
        if res and res.get("text"):
            return res
    arm = choose_arm(intent, arms)
    if not arm:
        return None
    out = _run_arm(arm, task, system, reasoning, timeout_s)
    if out:
        record_trial(intent, arm[0], arm[1], 1.0)     # produced a usable answer on exploit → a reliability "keep"
        return {"text": out, "lane": arm[0], "use_name": arm[1], "why": "exploit (learned)"}
    record_trial(intent, arm[0], arm[1], 0.0)         # it failed → drop its win-rate so a flaky arm falls out
    return None


def estimate_judge_cost(bakeoffs=(10, 100, 1000)):
    """ZERO-SPEND estimate of the bandit's ONLY real cost — the bake-off JUDGE. Per bake-off is ONE cheap judge call
    (the two lane answers are $0, plan-served). Prices the judge prompt (boilerplate + task + two answers, at the
    BOUNDED sizes the judge truncates to) + the tiny output cap, at the judge model's rate, then projects a monthly $
    for a few bake-off counts. No LLM is called — this is arithmetic over pricing.py."""
    from . import pricing
    judge = _bandit_judge_model()
    # the judge prompt caps: task ≤3000 + two answers ≤4000 each + ~300 boilerplate chars (see bakeoff_judge)
    in_chars = 300 + 3000 + 2 * 4000
    in_tok = in_chars // 4                                # ~4 chars/token; a conservative UPPER bound (answers are usually smaller)
    per = pricing.realtime_cost(judge, in_tok, _JUDGE_OUT_CAP)
    return {"judge_model": judge, "in_tok_bound": in_tok, "out_tok_cap": _JUDGE_OUT_CAP,
            "per_bakeoff_usd": per, "monthly": {n: (per * n if per is not None else None) for n in bakeoffs}}


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

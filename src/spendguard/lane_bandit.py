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
RUNAWAY_REWARD_DEFAULT = 0.5   # A2.3: an EXPLOIT answer that overshoots its arm's OWN measured output norm is a PARTIAL
#                                keep — usable but anomalously costly, penalized toward cheaper arms (not a failure=0.0)


def _bcfg(name, default):
    """A float advisor.* bandit knob (decay / epsilon / bake-off pacing), defaulted — every bandit parameter is CONFIG,
    never a hardcoded magic number. Delegates to config.advisor_float (the ONE advisor-float reader) so an explicit 0
    is honored (`v is None`, not `v or default`) and there is no divergent copy."""
    return config.advisor_float(name, default)


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


def _intent_realized_costs(intent, arms):
    """{(lane, use_name): realized $/call} for an intent — each arm's MEASURED mean output (which INCLUDES reasoning,
    since reasoning bills as output) priced at its rate, from the calls corpus. Only arms with measured output appear
    (a cold arm is absent). This is what makes a reasoning model that emits 7x on a small-norm intent genuinely more
    expensive than a lean one; it feeds the bake-off JUDGE (bakeoff_judge), which weighs that realized cost against
    quality AGENTICALLY — so the reasoning tax enters the learned reward as an LLM's judgement, not a routing formula.
    Never raises (a routing read must not break routing)."""
    costs = {}
    try:
        from . import calls, lane_catalog
        norms = calls.mean_out_by_executor_model(intent)   # {(executor_or_provider, model): {mean_out, n}}
        for arm in (arms or []):
            lane, use_name = arm
            base, _lv = lane_catalog.parse_use_name(use_name, lane)
            # match the (executor, model) the lane path RECORDS: suffix-style lanes (gemini) log the whole use-name
            model = use_name if lane_catalog.quirk(lane)["style"] == "suffix" else base
            rec = norms.get((lane, model))
            mean_out = (rec or {}).get("mean_out") or 0.0
            if mean_out <= 0:
                continue                                   # no measured output for this arm/intent → neutral tilt
            c = lane_catalog.use_name_cost(use_name, int(mean_out), int(mean_out), lane)   # price the REALIZED output
            if c:
                costs[arm] = float(c)
    except Exception:
        pass
    return costs


def arm_score(intent, arm):
    """Exploit ranking = the arm's AGENTIC value win-rate × a light idle-fill tilt. The win-rate is the bake-off JUDGE's
    decayed verdict, and that judge weighs quality-vs-cost AGENTICALLY (quality ~`advisor.bandit_quality_weight` / cost
    ~`advisor.bandit_cost_weight`, as PROMPT guidance — see bakeoff_judge). So cost already lives in this reward,
    decided by an LLM per bake-off, NOT by an arithmetic tradeoff here — a reasoning model that emits 7x the norm wins
    only when the judge found its quality worth the cost ("spend reasoning where it pays"). Routing itself stays PURE
    STATE (no LLM, no cost formula): it optimizes the learned value-reward, monotonic in the win-rate, so the
    equal-start / learned-winner invariant holds. `_idle_bonus` is a bounded capacity tilt toward idle paid plans
    (fills spare subscription capacity); it only breaks near-ties, never overrides a real value gap. Pure read."""
    lane, _use_name = arm
    wr = arm_stats(intent).get(arm, {}).get("winrate", 0.0)
    return wr * _idle_bonus(lane)


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
    return max(live, key=lambda a: arm_score(intent, a))   # exploit the learned value-reward (cost already in it via the judge)


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


def _value_judge_prompt(task, out_a, out_b, cost_a=None, cost_b=None):
    """Build the bake-off judge prompt. When BOTH arms' realized $/call are known, the judge decides the better VALUE
    CHOICE: quality weighted ~`advisor.bandit_quality_weight` (default 0.70) against cost ~`advisor.bandit_cost_weight`
    (default 0.30), given as EXPLICIT GUIDANCE the LLM applies with JUDGEMENT — a large quality gap or a high-stakes
    task may let quality dominate; near-equal answers prefer the cheaper. The weights STEER an agentic decision; they
    are NOT an arithmetic tradeoff (that is the whole point — the "is the quality lift worth the cost?" call is the
    LLM's). When costs are absent (a cold intent, or an unpriced arm) it is a pure-quality judge. Both answers are
    included WHOLE — truncating one could hide the tail where the two DIFFER and flip the verdict. Pure string build,
    no LLM, no spend."""
    a, b = (out_a or "").strip(), (out_b or "").strip()
    priced = cost_a is not None and cost_b is not None and (cost_a > 0 or cost_b > 0)
    if priced:
        w_q = config.advisor_float("bandit_quality_weight", 0.7)
        w_c = config.advisor_float("bandit_cost_weight", 0.3)
        q_pct, c_pct = round(w_q * 100), round(w_c * 100)
        head = ("Two assistants answered the SAME task. Decide which is the better CHOICE for the task — the better "
                f"VALUE. Weight QUALITY about {q_pct}% and COST about {c_pct}%. This is guidance for your JUDGEMENT, "
                "not arithmetic: if one answer is clearly better in quality, or the task is high-stakes, let quality "
                "dominate; if the two are close in quality, prefer the cheaper. Decide whether the better answer's "
                f"quality gain is worth its extra cost. Per-call cost on this task type: ANSWER A ≈ ${cost_a:.4f}, "
                f"ANSWER B ≈ ${cost_b:.4f}.\nReply with ONLY ONE WORD, nothing else: A, or B, or TIE.\n\n")
    else:
        head = ("Two assistants answered the SAME task. Which answer is better — more correct, complete, and on-format "
                "for the task? Reply with ONLY ONE WORD, nothing else: A, or B, or TIE.\n\n")
    return f"{head}TASK:\n{task}\n\n=== ANSWER A ===\n{a}\n\n=== ANSWER B ===\n{b}\n"


def bakeoff_judge(task, out_a, out_b, arm_a, arm_b, cost_a=None, cost_b=None):
    """AGENTIC 2-way VALUE judge: which arm is the better CHOICE for the task? When the two arms' realized $/call are
    given, the judge weighs quality-vs-cost AGENTICALLY (quality ~70% / cost ~30% as PROMPT guidance — see
    _value_judge_prompt), so the cost tradeoff enters the learned reward as an LLM's meaning call, never an arithmetic
    formula; without costs it is a pure-quality judge. Returns (winner_arm or None, reason). The DECISION is the LLM's —
    only the fixed A/B/TIE token is parsed. Caged under the meta intent (attributed, never recurses into the bandit); an
    EMPTY output loses by default, so no judge call is spent when a side is blank (or when the two are identical).
    No max_tokens is passed — the reply is one word (bills ~1 token), and adapters.call REFUSES an oversize pair loudly
    rather than silently clipping the evidence."""
    a, b = (out_a or "").strip(), (out_b or "").strip()
    if a and not b:
        return arm_a, "B empty (no judge spend)"
    if b and not a:
        return arm_b, "A empty (no judge spend)"
    if not a and not b:
        return None, "both empty"
    if a == b:
        return None, "identical outputs (no judge spend)"
    prompt = _value_judge_prompt(task, a, b, cost_a, cost_b)
    try:
        from . import adapters, calls, gate
        from .advisor import META                                   # ONE source of the meta-intent prefix
        with calls.context(intent=f"{META}:bandit-judge"):          # caged: attributed, never bandit-routed
            r = adapters.call(_bandit_judge_model(), prompt)
        txt = (r.get("text") or "").strip()
    except Exception as e:
        if gate.is_deliberate_stop(e):                 # a spend refusal / deadline is NOT a tie — propagate, never fail-open
            raise
        return None, None                              # a genuine judge FAILURE → NO verdict (reason None): the caller must
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
    est-value. Returns (text, out_tok): ('', 0) on any failure; out_tok is the completed call's output-token count
    (reasoning INCLUDED, since it bills as output) so the EXPLOIT path can detect a cost-runaway (A2.3)."""
    lane, use_name = arm
    try:
        from . import adapters, lane_catalog, calls
        import importlib
        prov = lane_catalog.lane_provider(lane)
        entry = adapters._LANES.get(prov)
        if not entry:
            return "", 0
        mod = importlib.import_module("." + entry[1], "spendguard")
        base, level = lane_catalog.parse_use_name(use_name, lane)
        # gemini carries effort in the model SUFFIX (pass the whole use-name); the others take it as `reasoning`
        model = use_name if lane_catalog.quirk(lane)["style"] == "suffix" else base
        cap = int(getattr(mod, "TIMEOUT_S", 300))
        r = mod.run_prompt(task, system=system, model=model, timeout=min(cap, int(timeout_s or cap)),
                           reasoning=(level or reasoning))
        if not isinstance(r, dict) or r.get("error") or not (r.get("text") or "").strip():
            return "", 0
        out_tok = int(r.get("out_tok") or 0)
        try:
            calls.record_call(prov, model, "subscription", 0.0, in_tok=r.get("in_tok", 0), out_tok=out_tok,
                         latency=r.get("latency"), executor=lane, effort=(level or reasoning))
        except Exception:
            pass
        return (r.get("text") or ""), out_tok
    except Exception:
        return "", 0


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
    out_a, _ = _run_arm(arm_a, task, system, reasoning, timeout_s)   # bake-off: out_tok unused here — the value JUDGE
    out_b, _ = _run_arm(arm_b, task, system, reasoning, timeout_s)   #   weighs realized cost (A2.3's overshoot penalty is exploit-only)
    costs = _intent_realized_costs(intent, [arm_a, arm_b])   # realized $/call (incl. reasoning) → the judge weighs VALUE
    winner, reason = bakeoff_judge(task, out_a, out_b, arm_a, arm_b, costs.get(arm_a), costs.get(arm_b))
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


def _exploit_overshoot_reward(intent, arm, out_tok, pre_norm):
    """The EXPLOIT-path reward for a USABLE lane answer — 1.0 normally, a PENALTY (`advisor.bandit_runaway_reward`) when
    the call's out_tok is a cost-RUNAWAY vs this arm's OWN measured mean output for the intent. This is guardrail-E's
    signal brought to the lane path: metered calls get it via adapters._call_guarded (bulkgate.check_runaway), but lane
    calls BYPASS that path, so a learned-winner arm that drifts into over-reasoning would keep scoring a full 1.0 while
    silently costing more. ARITHMETIC on billed tokens (tokens = $), never a content/quality judgement — a reply many
    times the arm's norm costs that multiple whether it is good or bad, and the bandit should re-explore cheaper arms.
    `pre_norm` is mean_out_by_executor_model(intent) captured BEFORE this call was recorded, so the call cannot inflate
    its own baseline. Cold / too-few-samples ⇒ 1.0 (never accuse on an untrustworthy norm; guardrail D bounds the $).
    Also SURFACES the trip via bulkgate.note_runaway so a lane runaway is visible + counted like a metered one. Never
    raises (a reward read must not break routing)."""
    try:
        if not out_tok or int(out_tok) <= 0:
            return 1.0
        from . import lane_catalog, bulkgate
        lane, use_name = arm
        base, _lv = lane_catalog.parse_use_name(use_name, lane)
        # the (executor, model) the lane path RECORDS under — suffix-style lanes (gemini) log the whole use-name
        model = use_name if lane_catalog.quirk(lane)["style"] == "suffix" else base
        rec = (pre_norm or {}).get((lane, model)) or {}
        mean_out, n = float(rec.get("mean_out") or 0.0), int(rec.get("n") or 0)
        if mean_out <= 0 or n < bulkgate.RUNAWAY_MIN_SAMPLES:
            return 1.0                                       # cold / unstable norm ⇒ cannot accuse (guardrail D bounds the $)
        if int(out_tok) > bulkgate._runaway_factor() * mean_out:
            try:                                             # SURFACE + count — closes "note_runaway never fires on lanes"
                bulkgate.note_runaway(bulkgate.sig(model, template_id=intent), model, int(out_tok), int(mean_out),
                                      "lane-exploit-mean")
            except Exception:
                pass
            return config.advisor_float("bandit_runaway_reward", RUNAWAY_REWARD_DEFAULT)
        return 1.0
    except Exception:
        return 1.0


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
    from . import calls
    pre_norm = calls.mean_out_by_executor_model(intent)   # PRE-call norm (this call not yet recorded) → no self-inflation
    out, out_tok = _run_arm(arm, task, system, reasoning, timeout_s)
    if out:
        won = _exploit_overshoot_reward(intent, arm, out_tok, pre_norm)   # 1.0, or a PENALTY on a cost-runaway (A2.3)
        record_trial(intent, arm[0], arm[1], won)     # usable answer → keep; a runaway keep is PARTIAL so cheaper arms overtake
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
    # the judge prompt caps: task ≤3000 + two answers ≤4000 each + ~700 boilerplate chars (the value-judge guidance +
    # the two per-call cost figures; the pure-quality variant is smaller — see _value_judge_prompt)
    in_chars = 700 + 3000 + 2 * 4000
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

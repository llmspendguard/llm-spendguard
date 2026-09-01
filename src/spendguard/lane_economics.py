"""Subscription ECONOMICS — turn each plan's opaque "% remaining" into the numbers that let the router (and the
operator) balance THOUGHTFULLY across plans: an absolute token CAP per window, how many tokens are actually LEFT,
the effective $/token the plan delivers, and the $ that will be WASTED if the unused allowance expires at reset.

WHY THIS EXISTS. A subscription is a flat fee F that grants a token cap C per window — sticker price F/C $/token.
Once subscribed, F is SUNK, so every token up to C is $0 at the margin (a plan call always beats a metered one).
But the flip side is the whole point of this module: every token of C you DON'T spend before the window resets is
FEE YOU PAID FOR NOTHING. Balancing across plans on `remaining_pct` alone is blind to cap SIZE — 14% of a large
plan can be more absolute tokens than 37% of a small one — so overflow gets water-filled the wrong way. This module
supplies the missing absolute axis.

HOW THE CAP IS MEASURED — never hardcoded, no window-length assumed. Providers expose a % remaining, not a token
count, so we back the cap out from real consumption: sample (remaining_pct, cumulative-tokens) per (lane, bucket)
over time; between two samples in the SAME window (same reset_ts, % dropped), the lane consumed Δtok tokens for a
Δfrac drop in the gauge, so

    cap  C ≈ Δtok / Δfrac                       (median over the valid sample pairs; self-calibrating)

That needs NOTHING about the window LENGTH — only that the gauge moved while we watched tokens flow. Until at least
one valid pair exists the cap is None ("estimating"), never an invented number (fail-open, the balances.py rule).

BUCKETS, not one cap. A plan exposes several windows at once (claude-code: a rolling session + a weekly cap +
a per-model weekly cap; gemini/Antigravity: separate weekly caps for Gemini vs Claude/GPT models). Each is its own
constraint with its own cap and reset, so the cap is estimated PER (lane, bucket); the lane's BINDING remaining is
the bucket with the fewest absolute tokens left (what throttles first). Bucket period (for the $/token proration)
comes from an OBSERVED reset-to-reset delta when we have one, else a keyword parse of the bucket name (a known
vocabulary — session/day/week/month — parsing a fixed shape, not deciding meaning), else None.

This module holds only measurement + arithmetic on provider-TRUTH numbers — no LLM, no meaning-judgement. The
per-lane fee comes from lane_balance._lane_fee (config subscription.lane_plans, else the plan total split, flagged
approximate). The metered cross-check (#5) prices the same tokens on the cheapest SUNK pool via pricing, so the
report can show what the plan SAVED versus paying per-token.
"""
import time

from . import config

# Per-(lane, bucket) sample history, persisted cross-process; bounded so state never grows without limit.
_SAMPLES_STATE = "lane_economics_samples"
_MAX_SAMPLES_PER_KEY = 60          # ~a month at the 0.5h headroom cadence; plenty of pairs, bounded size
_MIN_FRAC_DROP = 0.01              # ignore pairs whose gauge barely moved — dividing by a tiny Δfrac blows up C
_MIN_TOK_DELTA = 1                 # a pair must have real token flow to inform the cap

# Keyword → window length (days) for a bucket NAME. Parsing a KNOWN vocabulary (fixed shape), not deciding meaning;
# only used when we have not yet OBSERVED a reset-to-reset delta (which always wins). None-safe: unknown → None.
_PERIOD_KEYWORDS = (("session", 5.0 / 24.0), ("hour", 1.0 / 24.0), ("day", 1.0), ("daily", 1.0),
                    ("week", 7.0), ("weekly", 7.0), ("month", 30.0), ("monthly", 30.0))


def _bucket_key(lane, bucket_name):
    return f"{lane}\x1f{bucket_name}"


def _cum_tokens_by_executor():
    """{executor: cumulative in+out tokens over ALL recorded calls}. The cap estimate only ever uses the DIFFERENCE
    between two samples of this within one window, so an all-time baseline is fine (it cancels). Best-effort → {} on
    any error (the caller then simply records no fresh sample; it never breaks the headroom refresh)."""
    try:
        import sqlite3
        con = sqlite3.connect(config.db_path())
        rows = con.execute(
            "SELECT executor, SUM(COALESCE(in_tok,0)+COALESCE(out_tok,0)) FROM calls "
            "WHERE executor IS NOT NULL AND executor != '' GROUP BY executor").fetchall()
        con.close()
        return {ex: int(t or 0) for ex, t in rows}
    except Exception:
        return {}


def record_samples(headroom_rows):
    """Append one sample per (lane, bucket) from a lane_headroom() result — {ts, remaining_pct, reset_ts, cum_tok} —
    so estimate_cap() has a growing series to back the cap out of. Only KNOWN buckets with a remaining_pct are
    recorded (an unknown gauge tells us nothing). Bounded to _MAX_SAMPLES_PER_KEY per key. Persistence is a bonus:
    any error is swallowed so a bookkeeping failure never breaks the headroom read that called us."""
    try:
        cum = _cum_tokens_by_executor()
        now = time.time()
        st = config.load_state(_SAMPLES_STATE, {}) or {}
        series = dict(st.get("keys") or {})
        for r in (headroom_rows or []):
            lane = r.get("lane")
            tok = cum.get(lane)
            if tok is None:
                continue                                   # no token signal for this executor yet → nothing to anchor
            for b in (r.get("buckets") or []):
                pct = b.get("remaining_pct")
                if pct is None:
                    continue
                key = _bucket_key(lane, b.get("bucket") or "")
                lst = list(series.get(key) or [])
                last = lst[-1] if lst else None
                sample = {"ts": now, "remaining_pct": float(pct),
                          "reset_ts": (float(b["reset_ts"]) if b.get("reset_ts") else None), "cum_tok": int(tok)}
                # skip a duplicate that adds no information (same window, same gauge, same token count)
                if last and last.get("reset_ts") == sample["reset_ts"] \
                        and last.get("remaining_pct") == sample["remaining_pct"] \
                        and last.get("cum_tok") == sample["cum_tok"]:
                    continue
                lst.append(sample)
                series[key] = lst[-_MAX_SAMPLES_PER_KEY:]
        config.save_state(_SAMPLES_STATE, {"keys": series, "asof": now}, loud=False)
    except Exception:
        pass


def _bucket_samples(lane, bucket_name):
    st = config.load_state(_SAMPLES_STATE, {}) or {}
    return list((st.get("keys") or {}).get(_bucket_key(lane, bucket_name)) or [])


def _valid_pairs(samples):
    """Consecutive (earlier, later) sample pairs usable to estimate a cap: SAME window (same reset_ts, so no refill
    happened between them), the gauge DROPPED by at least _MIN_FRAC_DROP, and real tokens flowed. Yields
    (delta_tok, delta_frac) for each. Cross-reset pairs (a refill in between) are skipped — they would read as a huge
    negative consumption and are meaningless for the cap."""
    for a, b in zip(samples, samples[1:]):
        if a.get("reset_ts") != b.get("reset_ts"):
            continue
        dfrac = (float(a["remaining_pct"]) - float(b["remaining_pct"])) / 100.0
        dtok = int(b["cum_tok"]) - int(a["cum_tok"])
        if dfrac >= _MIN_FRAC_DROP and dtok >= _MIN_TOK_DELTA:
            yield dtok, dfrac


def _median(xs):
    s = sorted(xs)
    n = len(s)
    if n == 0:
        return None
    mid = n // 2
    return s[mid] if n % 2 else (s[mid - 1] + s[mid]) / 2.0


def estimate_cap(lane, bucket_name):
    """(cap_tokens, n_pairs) for one (lane, bucket): the MEDIAN of Δtok/Δfrac over the valid sample pairs — an
    absolute token allowance per window, measured from real consumption vs the gauge, no window length assumed.
    (None, 0) when there is not yet one clean pair (estimating — never an invented cap). n_pairs is the evidence
    count so callers can flag 'converging' vs 'measured'."""
    est = [dtok / dfrac for dtok, dfrac in _valid_pairs(_bucket_samples(lane, bucket_name))]
    if not est:
        return None, 0
    return _median(est), len(est)


def _observed_period_days(samples):
    """Window length in DAYS from an OBSERVED reset-to-reset delta (two distinct reset_ts in the series), or None.
    This is the truth when we have it — it beats any name-keyword guess."""
    resets = sorted({float(s["reset_ts"]) for s in samples if s.get("reset_ts")})
    if len(resets) < 2:
        return None
    deltas = [b - a for a, b in zip(resets, resets[1:]) if b - a > 0]
    d = _median(deltas)
    return (d / 86400.0) if d else None


def _bucket_period_days(bucket_name, samples):
    """Window length (days): the OBSERVED reset delta if we have one, else a keyword parse of the bucket name, else
    None. Keyword parse matches a fixed vocabulary — a known-shape parse, not a meaning decision."""
    obs = _observed_period_days(samples)
    if obs:
        return obs
    low = (bucket_name or "").lower()
    for kw, days in _PERIOD_KEYWORDS:
        if kw in low:
            return days
    return None


def economics(headroom_rows=None, fee_by_lane=None):
    """Per-lane subscription economics rows. For each ENABLED, quota-KNOWN lane: the binding bucket, its measured cap
    and absolute tokens left, the effective $/token, and the fee wasted if the remaining allowance expires unused.
    Returns [] for lanes with no quota surface (nothing to model) — they are handled by the call-volume proxy
    elsewhere, not invented here.

    Each row: {lane, provider, fee_month, fee_exact, buckets:[{bucket, remaining_pct, cap, n_pairs, period_days,
    remaining_abs, used_abs, eff_usd_per_tok, waste_at_reset, reset_ts}], binding, converged}. `binding` is the
    bucket dict with the FEWEST absolute tokens left (what throttles first); it carries the lane-headline remaining_abs.
    fee_by_lane overrides the config fee (test seam)."""
    from . import lanes, lane_balance
    rows = list(headroom_rows if headroom_rows is not None else lanes.lane_headroom(do_fetch=False))
    total_fee, _fee_default = _plan_total_fee()
    n_lanes = len([r for r in rows]) or 1
    out = []
    for r in rows:
        if not r.get("known"):
            continue
        lane = r["lane"]
        if fee_by_lane is not None:
            fee, fee_exact = float(fee_by_lane.get(lane, 0.0)), (lane in fee_by_lane)
        else:
            fee, fee_exact = lane_balance._lane_fee(lane, n_lanes, total_fee)
        bkts = []
        for b in (r.get("buckets") or []):
            name = b.get("bucket") or ""
            pct = b.get("remaining_pct")
            if pct is None:
                continue
            samples = _bucket_samples(lane, name)
            cap, n_pairs = estimate_cap(lane, name)
            frac = float(pct) / 100.0
            period_days = _bucket_period_days(name, samples)
            remaining_abs = (cap * frac) if cap is not None else None
            used_abs = (cap * (1.0 - frac)) if cap is not None else None
            # $/token = fee prorated to THIS window ÷ cap. Comparable across cadences (both scale with the period).
            eff = None
            if cap and cap > 0 and period_days:
                fee_period = fee * (period_days / 30.0)
                eff = fee_period / cap
            waste = (remaining_abs * eff) if (remaining_abs is not None and eff is not None) else None
            bkts.append({"bucket": name, "remaining_pct": int(pct), "cap": cap, "n_pairs": n_pairs,
                         "period_days": period_days, "remaining_abs": remaining_abs, "used_abs": used_abs,
                         "eff_usd_per_tok": eff, "waste_at_reset": waste, "reset_ts": b.get("reset_ts")})
        if not bkts:
            continue
        measured = [b for b in bkts if b["remaining_abs"] is not None]
        binding = (min(measured, key=lambda b: b["remaining_abs"]) if measured else None)
        out.append({"lane": lane, "meter": "tokens", "provider": r.get("provider"), "fee_month": round(fee, 2),
                    "fee_exact": fee_exact, "buckets": bkts, "binding": binding, "converged": bool(measured)})
    # PROMPT-metered lanes (no token gauge) get their native-unit row, keyed off the declared budget, not the headroom.
    prov_of = {r["lane"]: r.get("provider") for r in rows}
    for lane in _prompt_metered_lanes():
        if any(o["lane"] == lane for o in out):
            continue
        if fee_by_lane is not None:
            fee = float(fee_by_lane.get(lane, 0.0))
        else:
            fee, _ = lane_balance._lane_fee(lane, n_lanes, total_fee)
        pe = prompt_economics(lane, fee)
        if pe:
            pe["provider"] = prov_of.get(lane)
            out.append(pe)
    return out


def remaining_abs_by_lane(headroom_rows=None):
    """{lane: binding remaining_abs tokens or None} — the absolute-capacity axis the water-filling lane_score reads.
    None where the cap is not yet measured (the score then falls back to the fraction, unchanged)."""
    out = {}
    for e in economics(headroom_rows=headroom_rows):
        out[e["lane"]] = (e["binding"] or {}).get("remaining_abs") if e.get("binding") else None
    return out


def _bucket_pace(bucket, now):
    """PACE of one window: elapsed_frac − used_frac. POSITIVE = the plan is BEHIND pace (it has spent less than the
    fraction of the window that has elapsed → there is budget to spend NOW before it is wasted at reset, so route
    here). NEGATIVE = AHEAD of pace (spending faster than the clock → it will run out before reset, so shed off it).
    None when the window length or reset is not known yet (no pace signal → the router falls back to plain headroom).
    This is the axis that makes 'use every plan to ~100% of its window, never over' automatic and per-plan."""
    period_days = bucket.get("period_days")
    reset = bucket.get("reset_ts")
    pct = bucket.get("remaining_pct")
    if not period_days or not reset or pct is None:
        return None
    period_s = float(period_days) * 86400.0
    if period_s <= 0:
        return None
    elapsed = min(max((float(now) - (float(reset) - period_s)) / period_s, 1e-6), 1.0)
    used = 1.0 - float(pct) / 100.0
    return round(elapsed - used, 4)


def pace_by_lane(headroom_rows=None, now=None):
    """{lane: pace_headroom} over each lane's BINDING bucket (the one that throttles first) — the PACE axis the value
    router reads. Positive = behind pace (prefer routing here to use the paid capacity before it expires); negative =
    ahead (shed). None where the window is not measured yet. General: it paces claude/zai/gemini alike, so a plan the
    user is burning too fast (e.g. a nearly-exhausted weekly) self-identifies as ahead-of-pace with no special-casing."""
    now = now if now is not None else time.time()
    out = {}
    for e in economics(headroom_rows=headroom_rows):
        b = e.get("binding")
        out[e["lane"]] = (_bucket_pace(b, now) if b else None)
    return out


def pace_policy(lane):
    """The declared per-plan VALUE policy string for a lane, normalized — the ONE reader of config subscription.pace,
    so the router (route_utility._protect_policy) and the `lanes --economics` display never drift. 'maximize' (fill to
    ~100% of the window; the default for any unlisted lane) or 'protect'/'conservative' (shed discretionary work once
    ahead of pace, preserving the allowance for what only this plan can do)."""
    pace = config._cfg_get("subscription", "pace", None) or {}
    e = pace.get(lane) if isinstance(pace, dict) else None
    pol = (e.get("policy") if isinstance(e, dict) else e) if e is not None else None
    return str(pol or "maximize").strip().lower()


def pace_reserve_frac(lane):
    """The fraction of a lane's window to HOLD BACK — the router SHEDS the lane once its remaining capacity is at or
    below this, so a plan is used to ~100% of its window but never pushed PAST it (the hard cap the pace nudge lacks).
    Per-lane subscription.pace[lane].reserve_frac, else the global subscription.pace_reserve_frac, else 0.0 (use fully;
    shed only at exactly 0% — the plan's own boundary). Read HERE (the one reader of subscription.pace) so the router
    and the economics view never drift. Clamped to [0, 0.99] so a bad value can never make every lane unusable."""
    pace = config._cfg_get("subscription", "pace", None) or {}
    e = pace.get(lane) if isinstance(pace, dict) else None
    raw = e.get("reserve_frac") if isinstance(e, dict) else None
    if raw is None:
        raw = config._cfg_get("subscription", "pace_reserve_frac", 0.0)
    try:
        return max(0.0, min(0.99, float(raw or 0.0)))
    except Exception:
        return 0.0


def _plan_total_fee():
    """(total_monthly_plan_fee, is_default) — reuse the receipt's plan total so this equals what `lanes --balance`
    already shows. Best-effort → (0.0, True) so the model degrades to $/token=None rather than raising."""
    try:
        from . import receipt
        fee, default = receipt._plan_usd()
        return float(fee), bool(default)
    except Exception:
        return 0.0, True


def _metered_floor_cost(in_tok, out_tok):
    """(cost, model) — what `in_tok`+`out_tok` WOULD cost on the cheapest SUNK-pool metered model available, so the
    report can show the $ a plan SAVED vs paying per token (#5). Ranks the declared sunk pools by realtime price on a
    nominal split; None when nothing is priceable. Uses pricing only — no network, no LLM."""
    from . import pricing, balances
    best = None
    for vendor, model in _sunk_pool_models().items():
        try:
            if (balances.vendor_balance(vendor) or {}).get("kind") != "sunk_pool":
                continue
        except Exception:
            continue
        c = pricing.cost_or_unpriced(model, int(in_tok), int(out_tok), batch=False, provider=vendor)
        if c and (best is None or c < best[0]):
            best = (c, f"{vendor}:{model}")
    return best if best else (None, None)


def _sunk_pool_models():
    """{vendor: cheapest representative model} for the sunk pools we can price a comparison on. Config-overridable
    (balances.compare_models); the defaults name each pool's cheap tier, discovered from the live catalog, not a
    price literal."""
    cfg = config._cfg_get("balances", "compare_models", None)
    if isinstance(cfg, dict) and cfg:
        return dict(cfg)
    return {"deepseek": "deepseek-v4-flash", "gemini": "gemini-flash-latest", "moonshot": "kimi-k2.6"}


# ── PROMPT-METERED lanes (e.g. z.ai Coding Plan: a weekly PROMPT quota, not tokens, and NO usage API) ────────────────
# A prompt-metered plan sells requests, not tokens, and here exposes no gauge — so it cannot be token-water-filled like
# agy/claude. Two facts shape the model: (1) the budget is a DECLARED prompts/window (config, from published limits),
# and consumption is only what spendguard ITSELF sent (the user's direct coding on the same quota is invisible), so
# "remaining" is an over-estimate and is never presented as authoritative; (2) the plan STOPS DEAD at the cap (no
# overage, no grace), so the guard's job is to cap spendguard's OWN footprint — the one thing it can measure — leaving
# the rest of the scarce budget for real coding.

def _prompt_metered_lanes():
    """Lanes that have a valid DECLARED prompt budget — the single structural signal that a lane is prompt-metered
    (there is no separate meter-type flag to interpret). Iterates subscription.lane_prompt_budget and keeps the keys
    _prompt_budget accepts as well-formed."""
    b = config._cfg_get("subscription", "lane_prompt_budget", None) or {}
    return [ln for ln in b if _prompt_budget(ln) is not None] if isinstance(b, dict) else []


def _prompt_budget(lane):
    """{prompts, window_days} DECLARED prompt budget for a prompt-metered lane (config
    subscription.lane_prompt_budget[lane]), or None. Declared from the plan's published limits — not measured (no
    gauge exists) and not a source literal."""
    b = config._cfg_get("subscription", "lane_prompt_budget", None) or {}
    e = b.get(lane) if isinstance(b, dict) else None
    if isinstance(e, dict) and e.get("prompts") and e.get("window_days"):
        try:
            return {"prompts": int(e["prompts"]), "window_days": float(e["window_days"])}
        except (TypeError, ValueError):
            return None
    return None


def _prompts_consumed(lane, window_days):
    """Count of prompts SPENDGUARD sent to `lane` in the trailing window_days (one call = one prompt). This is a LOWER
    BOUND on true quota use — the user's direct coding on the same plan is invisible here. Best-effort → 0 on error."""
    try:
        import sqlite3
        con = sqlite3.connect(config.db_path())
        n = con.execute("SELECT COUNT(*) FROM calls WHERE executor = ? AND ts >= datetime('now', ?)",
                        (lane, f"-{float(window_days)} days")).fetchone()[0]
        con.close()
        return int(n or 0)
    except Exception:
        return 0


def _tokens_per_prompt(lane):
    """Measured mean (in+out) tokens per prompt for this lane's calls, or None if it has run none. Reflects the SIZE of
    the prompts spendguard sent — which is exactly the number that turns a $/prompt into a $/token."""
    try:
        import sqlite3
        con = sqlite3.connect(config.db_path())
        n, t = con.execute("SELECT COUNT(*), SUM(COALESCE(in_tok,0)+COALESCE(out_tok,0)) FROM calls "
                          "WHERE executor = ?", (lane,)).fetchone()
        con.close()
        return (int(t or 0) / int(n)) if n else None
    except Exception:
        return None


def _selfuse_cap_frac():
    """The fraction of a prompt-metered lane's budget spendguard will spend on its OWN discretionary work before it
    backs off (config advisor.prompt_lane_selfuse_cap_frac; default 0.25). A policy knob, like lane_balance_margin."""
    try:
        return float(config._cfg_get("advisor", "prompt_lane_selfuse_cap_frac", 0.25))
    except Exception:
        return 0.25


def prompt_economics(lane, fee):
    """Economics for one PROMPT-metered lane, or None when it has no declared budget. {lane, meter:'prompts', fee_month,
    budget_prompts, window_days, consumed_prompts (spendguard-visible), remaining_prompts, usd_per_prompt,
    tokens_per_prompt, usd_per_tok, selfuse_frac, reserved, visible_only:True}. remaining/consumed are spendguard's
    OWN view only (flagged), never the plan's true state."""
    budget = _prompt_budget(lane)
    if not budget:
        return None
    bp, wd = budget["prompts"], budget["window_days"]
    consumed = _prompts_consumed(lane, wd)
    remaining = max(0, bp - consumed)
    fee_window = float(fee) * (wd / 30.0)
    usd_per_prompt = (fee_window / bp) if bp else None
    tpp = _tokens_per_prompt(lane)
    usd_per_tok = (usd_per_prompt / tpp) if (usd_per_prompt and tpp) else None
    frac = _selfuse_cap_frac()
    return {"lane": lane, "meter": "prompts", "fee_month": round(float(fee), 2), "budget_prompts": bp,
            "window_days": wd, "consumed_prompts": consumed, "remaining_prompts": remaining,
            "usd_per_prompt": usd_per_prompt, "tokens_per_prompt": tpp, "usd_per_tok": usd_per_tok,
            "selfuse_frac": frac, "reserved": consumed >= frac * bp, "visible_only": True}


def prompt_lane_reserved(lane):
    """True when spendguard's OWN consumption on this prompt-metered lane has reached its self-use cap — the router
    then stops sending it DISCRETIONARY work, reserving the rest of the stops-dead budget for real coding. False for
    token-metered lanes, lanes with no budget, or when under the cap. Never raises (a bookkeeping error → False, the
    permissive direction, so a glitch can't wedge routing)."""
    try:
        if _prompt_budget(lane) is None:    # not prompt-metered → the reserve does not apply
            return False
        e = prompt_economics(lane, 0.0)     # fee irrelevant to the reserve decision (it's consumption vs budget)
        return bool(e and e["reserved"])
    except Exception:
        return False


def _used_split(lane):
    """(in_frac, out_frac) — this lane's real input/output token mix from the calls it has run, so the metered
    cross-check prices the SAME shape of traffic it actually served. (0.5, 0.5) when nothing is recorded yet (a
    neutral split, never a fabricated skew)."""
    try:
        import sqlite3
        con = sqlite3.connect(config.db_path())
        i, o = con.execute("SELECT SUM(COALESCE(in_tok,0)), SUM(COALESCE(out_tok,0)) FROM calls "
                           "WHERE executor = ?", (lane,)).fetchone()
        con.close()
        i, o = int(i or 0), int(o or 0)
        tot = i + o
        return (i / tot, o / tot) if tot else (0.5, 0.5)
    except Exception:
        return (0.5, 0.5)


def _fmt_tok(n):
    """Compact token count: 1.42M / 525K / 900."""
    if n is None:
        return "—"
    n = float(n)
    if n >= 1e6:
        return f"{n / 1e6:.2f}M"
    if n >= 1e3:
        return f"{n / 1e3:.0f}K"
    return f"{n:.0f}"


def format_economics(headroom_rows=None):
    """The `spendguard lanes --economics` view: per plan, the MEASURED token cap, tokens left, effective $/token, the
    fee at risk if the window resets unused, and what the same traffic WOULD have cost on the cheapest sunk-pool
    metered (so a plan's saving is visible). Read-only; no spend. Lanes with no quota surface are named, not faked.
    The evidence COUNT (consumption pairs) is shown raw so the reader judges sufficiency — no hard-coded 'measured'
    cutoff."""
    import datetime
    rows = economics(headroom_rows=headroom_rows)
    known_lanes = {e["lane"] for e in rows}
    lines = ["subscription economics — measured token caps · $/token · fee at risk this window",
             "  (cap MEASURED from consumption vs the plan gauge; fee split ÷lanes is approximate — set "
             "subscription.lane_plans for exact)"]
    for e in rows:
        star = "" if e.get("fee_exact", True) else "*"
        lines.append(f"\n  {e['lane']:<12} ({e['provider']})  fee ${e['fee_month']:.0f}/mo{star}")
        if e.get("meter") == "prompts":
            upp = f"${e['usd_per_prompt']:.4f}/prompt" if e.get("usd_per_prompt") is not None else "$?/prompt"
            ut = (f" · ${e['usd_per_tok'] * 1e6:.2f}/1M tok at {_fmt_tok(e['tokens_per_prompt'])} tok/prompt measured"
                  if e.get("usd_per_tok") is not None else "")
            lines.append(f"     PROMPT-metered: {e['budget_prompts']} prompts / {e['window_days']:.0f}d  ·  {upp}{ut}")
            lines.append(f"     spendguard used {e['consumed_prompts']} of them this window "
                         f"(self-view only — your direct coding is invisible; true remaining is lower)")
            if e.get("reserved"):
                lines.append(f"     ⛔ RESERVED — spendguard hit its {e['selfuse_frac']*100:.0f}% self-use cap; "
                             f"holding the rest for real coding (stops-dead plan)")
            continue
        b = e["binding"]
        if not b:
            pairs = max((bk["n_pairs"] for bk in e["buckets"]), default=0)
            lines.append(f"     cap estimating — need the gauge to move while tokens flow ({pairs} usable "
                         f"pair(s) so far); balancing on % until then")
            continue
        when = (" · resets " + datetime.datetime.fromtimestamp(b["reset_ts"]).strftime("%b %d")) if b.get("reset_ts") else ""
        lines.append(f"     binding: {b['bucket']}  —  {b['remaining_pct']}% left, "
                     f"{_fmt_tok(b['remaining_abs'])} of ~{_fmt_tok(b['cap'])} tok left{when}  "
                     f"(from {b['n_pairs']} consumption pair(s))")
        pace = _bucket_pace(b, time.time())
        if pace is not None:
            # Band by the SIGN only — the mathematical zero-crossing (elapsed_frac vs used_frac), not a hand-picked
            # cutoff; this is exactly the boundary the router uses (lane_score boosts iff pace > 0). The raw magnitude
            # is printed so the reader judges HOW far off pace it is — no threshold decides that for them.
            pol = pace_policy(e["lane"])
            protect = pol in ("protect", "conservative")
            if pace > 0:
                tag = "BEHIND pace — budget to spend before it resets (route fungible work here)"
            else:
                tag = ("AHEAD of pace — will exhaust before reset"
                       + (" → SHED (protected: held for its own window)" if protect
                          else " → eases off in ranking"))
            pol_txt = "" if pol == "maximize" else f"  [policy: {pol}]"
            lines.append(f"     pace {pace:+.2f}: {tag}{pol_txt}")
        if b["eff_usd_per_tok"] is not None:
            lines.append(f"     ${b['eff_usd_per_tok'] * 1e6:.2f} / 1M tok effective")
        if b["waste_at_reset"] is not None:
            in_f, out_f = _used_split(e["lane"])
            used = b["used_abs"] or 0.0
            saved, model = _metered_floor_cost(used * in_f, used * out_f)
            save_txt = (f" · used {_fmt_tok(used)} tok would cost ~${saved:.2f} metered ({model.split(':')[0]}) — "
                        f"plan covered it" if saved else "")
            lines.append(f"     ${b['waste_at_reset']:.2f} of fee AT RISK if the rest expires unused{save_txt}")
    idle = [ln for ln in (config._cfg_get("advisor", "delegate_lanes", None) or []) if ln not in known_lanes]
    if idle:
        lines.append(f"\n  {', '.join(idle)}: no plan quota surface exposed — economics via call-volume proxy only "
                     f"(no absolute cap to measure)")
    return "\n".join(lines)

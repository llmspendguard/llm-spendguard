"""The LANE-ELIGIBLE-BUT-METERED figure (defect 3): of the realtime metered tokens billed this period, how many were
INDEPENDENT ONE-SHOT COMPREHENSION that could have ridden a $0 subscription lane (or the half-price Batch API) but
paid the metered API anyway? That is the only number that says whether rule #8 (lane-routed comprehension — the
biggest cost lever when the plan is capped) is actually in force. The existing "fallback-spend" line ($2.96) answers
a DIFFERENT question (spend that fell over from a lane the router already tried and found down) and must not be read
as this one.

Whether a given job is lane-eligible is a JUDGEMENT about the work's SHAPE — independent one-shot comprehension vs a
latency-sensitive interactive/agentic turn — so it is decided AGENTICALLY, never by a regex or an intent-prefix
allowlist (doctrine). It reuses the vetted judge prompts._batchable_verdict (batchable ⇔ independent one-shot ⇔
lane-eligible), caches the verdict PER INTENT (one bounded, meta-capped judgement per distinct intent, not per row),
and the spend is OPT-IN (execute=True) and ESTIMATE-FIRST — the $0 surfaces read only cached verdicts. Structural
exclusions (embeddings by kind, the Batch-API paths, spendguard's own meta calls) are PARSING on fixed fields, not a
meaning judgement; only the independent-one-shot-vs-interactive call is agentic."""
import datetime

from . import config, calls

_CACHE_KEY = "lane_eligibility_verdicts"      # SPENDGUARD_HOME state: {intent: {eligible, why, model, asof}}
_VERDICT_PROMPT_TOK_EST = 90                  # the judge prompt is a fixed shape (~90 input tokens) — for the pre-spend ESTIMATE only


def _now_iso():
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")


def _load_verdict_cache():
    d = config.load_state(_CACHE_KEY, default=None)
    return dict(d) if isinstance(d, dict) else {}


def _save_verdict_cache(cache):
    try:
        config.save_state(_CACHE_KEY, cache, loud=False)
    except Exception:
        pass                                  # the cache is a bonus; never fail a report over a persistence hiccup


def _judge_unit_cost():
    """Estimated $ for ONE per-intent agentic verdict on the advisor JUDGE model — a fixed-shape prompt
    (~_VERDICT_PROMPT_TOK_EST in) plus the small schemaed JSON out. Priced from pricing.py only; 0.0 when the judge
    model is unpriced (the estimate then under-counts rather than inventing a price). Pre-spend ESTIMATE only."""
    try:
        from . import pricing, prompts
        p = pricing.price(config.advisor_judge_model()) or {}
        in_per = float(p.get("in_") or 0) / 1e6
        out_per = float(p.get("out") or 0) / 1e6
        return _VERDICT_PROMPT_TOK_EST * in_per + int(getattr(prompts, "_BATCH_JUDGE_OUT", 120)) * out_per
    except Exception:
        return 0.0


def classify_intent(intent, model, n, avg_in, execute=False):
    """Cached per-intent lane-eligibility verdict {eligible, why, model, asof}, or None when unjudged and execute is
    False. A cache hit is $0. With execute=True an unjudged intent is judged ONCE (prompts._batchable_verdict — a
    meta-capped agentic call) and the verdict cached. A deliberate spend refusal propagates (never a silent skip)."""
    key = intent or "(none)"
    cache = _load_verdict_cache()
    hit = cache.get(key)
    if isinstance(hit, dict) and isinstance(hit.get("eligible"), bool):
        return hit
    if not execute:
        return None
    from . import prompts
    v = prompts._batchable_verdict(intent, model, n, avg_in)   # agentic: independent one-shot (lane/batch-eligible) vs interactive
    if not (isinstance(v, dict) and isinstance(v.get("batchable"), bool)):
        return None
    rec = {"eligible": bool(v["batchable"]), "why": (v.get("why") or "")[:200], "model": model, "asof": _now_iso()}
    cache[key] = rec
    _save_verdict_cache(cache)
    return rec


def lane_eligible_report(since=None, execute=False):
    """{"eligible_usd", "eligible_tok", "eligible_calls", "total_metered_usd", "judged", "unjudged",
    "unjudged_usd", "est_judge_usd", "by_intent": [...]} for realtime metered spend since `since` (default: month
    start). $0 with execute=False (reads only cached verdicts; counts how many intents remain unjudged and what it
    would cost to classify them). With execute=True it judges the unjudged intents (opt-in spend, cached). The figure
    is intent-folded (summed across models); a representative model + average input size drive the judge prompt."""
    since = since or config.month_start_utc()
    rows = calls.metered_realtime_by_intent(since)
    folded = {}
    for r in rows:
        agg = folded.setdefault(r["intent"], {"usd": 0.0, "calls": 0, "in_tok": 0, "model": r["model"]})
        agg["usd"] += r["usd"]
        agg["calls"] += r["calls"]
        agg["in_tok"] += r["in_tok"]
        # representative model = the one carrying the most $ (rows arrive biggest-$ first, so the first seen wins)
    total = round(sum(a["usd"] for a in folded.values()), 2)
    eligible_usd = eligible_tok = eligible_calls = 0.0
    judged = unjudged = 0
    unjudged_usd = 0.0
    details = []
    for intent, agg in sorted(folded.items(), key=lambda kv: -kv[1]["usd"]):
        avg_in = int(agg["in_tok"] / agg["calls"]) if agg["calls"] else 0
        v = classify_intent(intent, agg["model"], agg["calls"], avg_in, execute=execute)
        if v is None:
            unjudged += 1
            unjudged_usd += agg["usd"]
            continue
        judged += 1
        if v["eligible"]:
            eligible_usd += agg["usd"]
            eligible_tok += agg["in_tok"]
            eligible_calls += agg["calls"]
        details.append({"intent": intent, "usd": round(agg["usd"], 2), "calls": agg["calls"],
                        "eligible": v["eligible"], "why": v.get("why", "")})
    return {"eligible_usd": round(eligible_usd, 2), "eligible_tok": int(eligible_tok),
            "eligible_calls": int(eligible_calls), "total_metered_usd": total,
            "judged": judged, "unjudged": unjudged, "unjudged_usd": round(unjudged_usd, 2),
            "est_judge_usd": round(unjudged * _judge_unit_cost(), 4),
            "by_intent": sorted(details, key=lambda d: -d["usd"])}


def summary_line(since=None):
    """A $0 one-line surface for lanes --usage / receipt (cached verdicts only). None when there is no realtime metered
    spend to report. Names the lane-eligible-but-metered $, and — when intents are still unjudged — what it would cost
    to classify them, so the figure's COVERAGE is honest and the spend stays opt-in."""
    rep = lane_eligible_report(since=since, execute=False)
    if rep["total_metered_usd"] <= 0:
        return None
    head = (f"lane-eligible metered this period: ${rep['eligible_usd']:.2f} of ${rep['total_metered_usd']:.2f} "
            f"realtime metered was independent one-shot work that could ride a $0 lane / the Batch API")
    if rep["unjudged"]:
        head += (f" — {rep['unjudged']} intent(s) (${rep['unjudged_usd']:.2f}) UNJUDGED; classify with "
                 f"`spendguard lanes --judge-eligibility` (est ${rep['est_judge_usd']:.4f})")
    return head

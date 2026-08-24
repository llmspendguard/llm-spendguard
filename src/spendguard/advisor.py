"""Layer 2 — the LEARNING ADVISOR (its own LLM use, caged by caps.meta + intent spendguard:*).

Three operations, each ESTIMATE-FIRST by default (a separate zero-spend projection); spending
requires an explicit --run, and every paid call is tagged intent='spendguard:*' so it hits the
SEPARATE meta budget (config.meta_cap, default $2/day) and is excluded from the corpus it analyzes:

  reconstruct  bulk quality JUDGE (config.advisor_judge_model, Batch API) — label unlabeled calls
               that have stored prompt+output snippets ('was this output a usable result?').
  mine         insight SYNTHESIS (config.advisor_model, realtime) — roll the deterministic evidence
               into confidence-scored insights + learning-graph edges.
  optimize     interactive RECOMMENDATION (config.advisor_model, realtime) — per-intent advice that
               cites the evidence + mined insights.

The model for each role is configurable (see config.advisor_model / advisor_judge_model). The
estimate path makes ZERO paid calls — it only counts tokens and prices via pricing.py.
"""
import json
from . import calls, learn, config, pricing
from .submit import _count_tokens

META = "spendguard"   # intent prefix → routed to the meta budget by the gate

_JUDGE_SYS = ("You evaluate whether an LLM OUTPUT is a usable, correct result for its PROMPT. "
              "Reply with exactly one word: GOOD or BAD.")
_JUDGE_OUT_CEILING = 8          # the verdict is one token; tiny ceiling keeps the estimate honest

_MINE_SYS = ("You are a cost/quality analyst for LLM usage. Given a table of per-(intent,model) "
             "spend and quality evidence, output STRICT JSON and NOTHING else (no code fences): a list "
             'of AT MOST 6 objects {"intent": str|null, "lesson": str, "confidence": 0..1, "evidence": str}, '
             "most important first. Each lesson must be specific and actionable (which model/approach is "
             "cheaper per good result, where packing/batching would help). Keep each lesson under 240 "
             "characters. Only claim what the evidence supports; lower confidence when labels are sparse.")
_MINE_OUT = 1500

_OPT_SYS = ("You are a cost optimization advisor. Given historical evidence and mined insights for an "
            "intent, recommend how to run the next job most cheaply WITHOUT losing quality. Be concrete "
            "(model, batch vs realtime, packing, max_tokens). Note confounds. Keep it under 200 words.")
_OPT_OUT = 600

_REC_SYS = ("You are a model-SELECTION advisor. Given per-(vendor:model) cost×quality evidence for a job-type "
            "(intent), do two things and return STRICT JSON only (no prose, no code fences): "
            "(1) infer the QUALITY BAR this job needs — how much precision/accuracy matters relative to cost — "
            'as {"level": one of "precision-critical"|"balanced"|"cost-first", "why": short reason} from the '
            "intent name and the evidence, UNLESS the caller states a bar (then honor it); "
            "(2) rank the best models on the COST-vs-QUALITY frontier: the CHEAPEST option that still MEETS the "
            "bar first, then the next-best trade-offs. Do not recommend a pricier model unless it buys quality "
            'the job actually needs. Return {"quality_bar": {...}, "top": [{"id": "vendor:model", "why": reason '
            "<=160 chars, \"meets_bar\": bool}]}. Rank ONLY models present in the evidence; at most K entries.")
_REC_OUT = 700
_REC_SCHEMA = {"type": "object", "additionalProperties": False, "required": ["quality_bar", "top"], "properties": {
    "quality_bar": {"type": "object", "additionalProperties": False, "required": ["level", "why"],
                    "properties": {"level": {"type": "string"}, "why": {"type": "string"}}},
    "top": {"type": "array", "items": {
        "type": "object", "additionalProperties": False, "required": ["id", "why", "meets_bar"],
        "properties": {"id": {"type": "string"}, "why": {"type": "string"}, "meets_bar": {"type": "boolean"}}}}}}


# ─────────────────────────────── shared helpers ───────────────────────────────
def _judge_sample(per, limit=None, rows=None):
    """Unjudged call_io samples, bounded to `per` per (intent, model) — enough to estimate good%
    with a confidence interval, not label everything (keeps the judge spend small). `rows` lets the caller pass an
    already-fetched callio.unjudged() so it isn't queried twice."""
    from . import callio
    from collections import defaultdict
    if rows is None:
        rows = callio.unjudged()
    seen, out = defaultdict(int), []
    for io_id, intent, model, p, o in rows:
        if seen[(intent, model)] >= per:
            continue
        seen[(intent, model)] += 1
        out.append((io_id, intent, model, p, o))
        if limit and len(out) >= limit:
            break
    return out


def _judge_prompt(prompt_snip, output_snip):
    return f"PROMPT:\n{prompt_snip}\n\nOUTPUT:\n{output_snip}"


def _evidence_table(intent=None, top=40):
    """Compact per-(intent, model) evidence — the reasoner's input (so it sees reconstructed intents,
    e.g. phase_taxonomy=$1127, not just model totals). Cheap; no PII beyond model/intent labels."""
    rows = calls.summary(intent)   # (intent, model, jobs, cost, good, bad) — excludes meta
    if not rows:
        return None, 0
    rows = sorted(rows, key=lambda r: -(r[3] or 0))[:top]
    lines = ["intent | model | jobs | $total | good% | $/good"]
    for it, model, jobs, cost, good, bad in rows:
        # NORMALISE ONCE, THEN USE THE NORMALISED VALUES. `labeled` coerced None to 0 and the line below
        # then divided the RAW `good`, so a row with good=None and bad>0 produced a non-zero denominator
        # and a TypeError on the numerator — the one combination the coercion was added for.
        good_n, bad_n = (good or 0), (bad or 0)
        labeled = good_n + bad_n
        goodpct = f"{100*good_n/labeled:.0f}%" if labeled else "—"
        # `good` guards the DIVISION (a zero denominator is impossible arithmetic, not a missing value),
        # but `cost` was used raw beside it: a None cost with good>0 raised mid-render and took the whole
        # advisor table down. Two different questions that shared one guard.
        per_good = f"${(cost or 0)/good:.4f}" if good else "—"
        lines.append(f"{it} | {model} | {jobs} | ${cost or 0:.2f} | {goodpct} | {per_good}")
    return "\n".join(lines), len(rows)


def _est_line(mode, model, n, in_tok, out_tok, cost):
    print(f"  {mode:<8} {model:<22} {n:>5} call(s) · in~{in_tok:,} out≤{out_tok:,} -> ~${cost:.4f}")


# ─────────────────────────────── reconstruct (judge) ───────────────────────────────
def reconstruct(run=False, per=15, limit=None):
    """Judge a bounded sample of the recovered call_io corpus for quality → makes good% / $/good real.
    Estimate-only unless run=True. Realtime judge (synchronous, small, caged by intent spendguard:*)."""
    from . import callio
    judge = config.advisor_judge_model()
    unjudged = list(callio.unjudged())                    # fetch ONCE — reused for the sample AND the count (was
    samples = _judge_sample(per, limit, rows=unjudged)    # two separate callio.unjudged() DB queries, risking drift)
    total_unjudged = len(unjudged)
    print(f"reconstruct — quality judge = {judge} (realtime), caged by intent {META}:*")
    print(f"  call_io samples: {total_unjudged:,} unjudged   judging up to {per}/(intent,model) → {len(samples):,}")
    if not samples:
        print("  → no recovered prompt+output samples to judge. Run `spendguard fetch-io` first "
              "(recovers them from the providers, free). 0 spend.")
        return dict(requests=0, cost=0.0, model=judge)

    in_tok = sum(_count_tokens(_JUDGE_SYS + _judge_prompt(p, o), judge) for _, _, _, p, o in samples)
    out_tok = _JUDGE_OUT_CEILING * len(samples)
    cost = pricing.realtime_cost(judge, in_tok, out_tok)
    print("  ESTIMATE (zero paid calls):")
    _est_line("realtime", judge, len(samples), in_tok, out_tok, cost)
    print(f"  meta budget: ${config.meta_cap():.2f}/day · spent today {_meta_spent_str()}")
    if not run:
        from . import ui; ui.estimate_only(action="judge output quality", cost=cost)
        return dict(requests=len(samples), in_tok=in_tok, out_tok=out_tok, cost=cost, model=judge)

    from . import adapters
    good = bad = err = 0
    with calls.context(intent=f"{META}:reconstruct"):
        for io_id, _intent, _model, p, o in samples:
            try:
                r = adapters.call(judge, _judge_prompt(p, o), max_tokens=_JUDGE_OUT_CEILING, system=_JUDGE_SYS)
                if r["error"]:
                    err += 1
                    continue
                ok = (r["text"] or "").strip().upper().startswith("GOOD")
                callio.set_quality(io_id, ok, src="judge", conf=0.95)
                good += int(ok)
                bad += int(not ok)
            except Exception as e:
                # a mid-loop EXCEPTION (adapter raise, DB write) must not abort the WHOLE reconstruct and waste the
                # judgements ALREADY paid for + persisted this run (set_quality commits per sample). Count + go on.
                err += 1
                import sys as _sys
                _sys.stderr.write(f"[advisor:reconstruct] sample {io_id} skipped ({type(e).__name__}: {str(e)[:60]})\n")
    print(f"  judged {good + bad} ({err} errors): {good} good / {bad} bad.")
    print("  empirical quality per (intent, model):")
    for (intent, model), d in sorted(callio.good_rates().items(), key=lambda kv: -(kv[1]['judged'])):
        if d["judged"]:
            print(f"    {(intent or '(none)')[:22]:<24}{model[:20]:<22}"
                  f"good={('%.0f%%' % (100*d['good_rate'])) if d['good_rate'] is not None else '—':>5}"
                  f"  ({d['judged']}/{d['sampled']} judged)")
    return dict(judged=good + bad, good=good, bad=bad, model=judge)


# ─────────────────────────────── mine (insights) ───────────────────────────────
def mine(run=False, intent=None):
    model = config.advisor_model()
    table, n = _evidence_table(intent)
    print(f"mine — insight synthesis = {model} (realtime), caged by intent {META}:*")
    if not table:
        print("  → no evidence yet. Run `spendguard backfill` and record some calls first. 0 spend.")
        return dict(requests=0, cost=0.0, model=model)
    prompt = f"Evidence ({n} model rows){' for intent ' + intent if intent else ''}:\n{table}"
    in_tok = _count_tokens(_MINE_SYS + prompt, model)
    cost = pricing.realtime_cost(model, in_tok, _MINE_OUT)
    print("  ESTIMATE (zero paid calls):")
    _est_line("realtime", model, 1, in_tok, _MINE_OUT, cost)
    print(f"  meta budget: ${config.meta_cap():.2f}/day · spent today {_meta_spent_str()}")
    if not run:
        from . import ui; ui.estimate_only(action="synthesize the insights", cost=cost)
        return dict(requests=1, in_tok=in_tok, out_tok=_MINE_OUT, cost=cost, model=model)

    from . import adapters
    with calls.context(intent=f"{META}:mine"):
        r = adapters.call(model, prompt, max_tokens=_MINE_OUT, system=_MINE_SYS)  # gate → meta cap
    if r["error"]:
        print(f"  ERROR: {r['error']}")
        return dict(error=r["error"])
    added = _persist_insights(r["text"])
    print(f"  synthesized {added} insight(s) → learn.insights + graph. Cost ${r['cost']:.4f}.")
    return dict(insights=added, cost=r["cost"], model=model)


def _parse_insights(text):
    """Robustly extract a JSON insight list — tolerate ```json fences and max_tokens truncation."""
    import re
    t = re.sub(r"\s*```$", "", re.sub(r"^```(?:json)?\s*", "", text.strip()))
    s = t.find("[")
    if s < 0:
        return None
    frag = t[s:]
    candidates = []
    e = frag.rfind("]")
    if e >= 0:
        candidates.append(frag[:e + 1])
    cut = frag.rfind("}")                       # truncated array → close after last complete object
    if cut >= 0:
        candidates.append(frag[:cut + 1] + "]")
    for c in candidates:
        try:
            d = json.loads(c)
            if isinstance(d, list):
                return d
        except Exception:
            pass
    return None


def _persist_insights(text):
    data = _parse_insights(text)
    if data is None:
        learn.add_insight(None, text.strip()[:500], source="mined", confidence=0.4)
        return 1
    added = 0
    def _conf(v, default=0.5):
        """A confidence the model returned as null, "high" or "" is UNSTATED, not a crash. float(None)
        raises TypeError, and .get's default only applies when the KEY IS ABSENT — an explicit null still
        reaches float(). One malformed field aborted the whole insight-mining pass, discarding every
        lesson after it in a batch that was already paid for."""
        try:
            f = float(v)
        except (TypeError, ValueError):
            return default
        return min(1.0, max(0.0, f))

    for it in data if isinstance(data, list) else []:
        if not isinstance(it, dict) or not it.get("lesson"):
            continue
        iid = learn.add_insight(it.get("intent"), str(it["lesson"])[:500],
                                evidence=str(it.get("evidence", ""))[:500], source="mined",
                                confidence=_conf(it.get("confidence")))
        learn.add_node("insight", str(it["lesson"])[:80], attrs={"confidence": it.get("confidence")}, id=iid)
        if it.get("intent"):
            learn.add_edge(iid, it["intent"], "concerns")
        added += 1
    return added


# ─────────────────────────────── optimize (recommend) ───────────────────────────────
def optimize(intent=None, plan=None, run=False):
    model = config.advisor_model()
    table, n = _evidence_table(intent)
    ins = learn.insights(intent=intent)
    print(f"optimize — recommendation = {model} (realtime), caged by intent {META}:*")
    if not table:
        print("  → no evidence yet. Run `spendguard backfill` / record calls first. 0 spend.")
        return dict(requests=0, cost=0.0, model=model)
    ins_txt = "\n".join(f"- ({c:.2f}) {lesson}" for _i, lesson, _s, c, _e in ins[:12]) or "(none yet — run `spendguard mine`)"
    prompt = (f"Intent: {intent or 'all'}\nPlanned model: {plan or 'unspecified'}\n\n"
              f"Evidence:\n{table}\n\nMined insights:\n{ins_txt}\n\n"
              f"Recommend how to run the next job most cheaply without losing quality.")
    in_tok = _count_tokens(_OPT_SYS + prompt, model)
    cost = pricing.realtime_cost(model, in_tok, _OPT_OUT)
    print("  ESTIMATE (zero paid calls):")
    _est_line("realtime", model, 1, in_tok, _OPT_OUT, cost)
    print(f"  meta budget: ${config.meta_cap():.2f}/day · spent today {_meta_spent_str()}")
    if not run:
        from . import ui; ui.estimate_only(action="produce the recommendation", cost=cost)
        return dict(requests=1, in_tok=in_tok, out_tok=_OPT_OUT, cost=cost, model=model)

    from . import adapters
    with calls.context(intent=f"{META}:optimize"):
        r = adapters.call(model, prompt, max_tokens=_OPT_OUT, system=_OPT_SYS)
    if r["error"]:
        print(f"  ERROR: {r['error']}")
        return dict(error=r["error"])
    print("\n" + "─" * 60 + f"\n{r['text']}\n" + "─" * 60)
    print(f"(via {model}, ${r['cost']:.4f}; history proposes, `spendguard compare` disposes.)")
    return dict(cost=r["cost"], model=model, text=r["text"])


def recommend_models(intent=None, k=5, quality_bar=None, run=False):
    """Agentic TOP-K model recommendation for an intent, on the cost×quality frontier with an intent-set quality
    BAR. The structured (schema-forced) cousin of optimize: the reasoner infers how much precision the job needs
    (unless `quality_bar` is given), then ranks the CHEAPEST models that MEET that bar first. It ranks only
    models that have evidence for the intent — a bakeoff is what fills in untried ones. Estimate-FIRST: run=False
    returns the zero-spend estimate + candidate set; run=True makes ONE meta-caged realtime call (intent
    spendguard:recommend). Returns a structured dict (quality_bar + top[] enriched with the measured $/good)."""
    from . import advise
    model = config.advisor_model()
    ev = advise.ranked(intent=intent)
    if not ev["models"]:
        return dict(intent=intent, requests=0, cost=0.0, model=model, top=[],
                    note="no evidence yet for this intent — record calls or run a bakeoff first. 0 spend.")
    lines = ["id | jobs | $total | $/M-out | good% | $/good"]      # the same numbers advise shows, compacted
    for m in ev["models"]:
        gp = f"{100*m['good_rate']:.0f}%" if m["good_rate"] is not None else "—"
        pg = f"${m['per_good']:.4f}" if m["per_good"] is not None else "—"
        pm = f"${m['per_m_out']:.2f}" if m["per_m_out"] is not None else "—"
        lines.append(f"{m['id']} | {m['jobs']} | ${m['cost']:.2f} | {pm} | {gp} | {pg}")
    bar_txt = f"The caller REQUIRES this quality bar: {quality_bar}\n" if quality_bar else ""
    prompt = (f"Intent (job-type): {intent or 'all'}\nReturn AT MOST {int(k)} models.\n{bar_txt}\n"
              f"Cost×quality evidence — rank ONLY these candidates:\n" + "\n".join(lines))
    in_tok = _count_tokens(_REC_SYS + prompt, model)
    cost = pricing.realtime_cost(model, in_tok, _REC_OUT)
    if not run:
        return dict(intent=intent, requests=1, in_tok=in_tok, out_tok=_REC_OUT, cost=cost, model=model,
                    estimate_only=True, candidates=[m["id"] for m in ev["models"]],
                    note=f"estimate only (~${cost:.4f}); call with run=True to produce the ranking (meta-caged).")
    from . import adapters
    with calls.context(intent=f"{META}:recommend"):
        r = adapters.call(model, prompt, max_tokens=_REC_OUT, system=_REC_SYS, schema=_REC_SCHEMA)
    if r.get("error"):
        return dict(intent=intent, error=r["error"], model=model, cost=r.get("cost"))
    import json as _json
    try:
        parsed = r.get("json") if isinstance(r.get("json"), dict) else _json.loads(r["text"])
    except Exception:
        return dict(intent=intent, model=model, cost=r.get("cost"), quality_bar=None, top=[],
                    note="the reasoner's output did not parse as the expected shape.", raw=r.get("text"))
    by_id = {m["id"]: m for m in ev["models"]}
    top = []
    for t in (parsed.get("top") or [])[:int(k)]:
        m = by_id.get(t.get("id"), {})                            # enrich the pick with its MEASURED numbers
        top.append({"id": t.get("id"), "why": t.get("why"), "meets_bar": t.get("meets_bar"),
                    "per_good": m.get("per_good"), "good_rate": m.get("good_rate"),
                    "per_m_out": m.get("per_m_out"), "cost": m.get("cost"), "jobs": m.get("jobs")})
    return dict(intent=intent, model=model, cost=r.get("cost"), quality_bar=parsed.get("quality_bar"),
                top=top, ranked_from=len(ev["models"]),
                note="ranked from MEASURED evidence; models you haven't run are added by a bakeoff.")


# ─────────────────────────────── misc ───────────────────────────────
def _meta_spent_str():
    """The spent-today figure for display: a dollar amount, or an explicit UNKNOWN — never a $0 that only
    means the read failed."""
    v = _meta_spent()
    return "UNKNOWN (ledger unreadable)" if v is None else f"${v:.4f}"


def _meta_spent():
    """Meta spend so far today, or None if it cannot be READ.

    This returned 0.0 on any failure. Its three callers all DISPLAY the number next to the day's cap —
    enforcement lives elsewhere — so the consequence is not that the cap opens, it is that the operator is
    told "spent today $0.0000" by a machine that could not read the ledger, and then decides whether to
    approve a run on the strength of it. A figure nobody could read is UNKNOWN, and saying so costs one
    line."""
    from . import budget
    try:
        return budget.meta_spent_today()
    except Exception as e:
        from . import config
        config.warn_once(f"[spendguard] meta spend for today could not be read ({type(e).__name__}) — "
                         f"treating the cap as UNKNOWN, not as $0 spent.")
        return None


def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(prog="spendguard")
    ap.add_argument("op", choices=["reconstruct", "mine", "optimize"])
    ap.add_argument("--intent")
    ap.add_argument("--plan", help="(optimize) the model you're about to use")
    ap.add_argument("--per", type=int, default=15, help="(reconstruct) samples to judge per (intent,model)")
    ap.add_argument("--limit", type=int, help="(reconstruct) overall cap on samples to judge")
    ap.add_argument("--run", action="store_true", help="actually spend (default: estimate only). Capped by caps.meta.")
    a = ap.parse_args(argv)
    if a.op == "reconstruct":
        reconstruct(run=a.run, per=a.per, limit=a.limit)
    elif a.op == "mine":
        mine(run=a.run, intent=a.intent)
    else:
        optimize(intent=a.intent, plan=a.plan, run=a.run)
    return 0

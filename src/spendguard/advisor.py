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
from . import calls, learn, advise, config, pricing
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


# ─────────────────────────────── shared helpers ───────────────────────────────
def _judgeable():
    """Unlabeled, non-meta calls that have BOTH prompt and output snippets stored (judge needs them)."""
    with calls._lock:
        return calls._db().execute(
            "SELECT id, intent, model, prompt_snip, output_snip FROM calls "
            "WHERE quality IS NULL AND prompt_snip IS NOT NULL AND output_snip IS NOT NULL "
            "AND (intent IS NULL OR intent NOT LIKE 'spendguard:%')").fetchall()


def _unlabeled_count():
    with calls._lock:
        return calls._db().execute(
            "SELECT COUNT(*) FROM calls WHERE quality IS NULL "
            "AND (intent IS NULL OR intent NOT LIKE 'spendguard:%')").fetchone()[0]


def _judge_prompt(prompt_snip, output_snip):
    return f"PROMPT:\n{prompt_snip}\n\nOUTPUT:\n{output_snip}"


def _evidence_table(intent=None):
    """Compact text of the deterministic evidence — the reasoner's input (cheap, no PII beyond models)."""
    agg = advise.evidence(intent=intent)
    if not agg:
        return None, 0
    lines = ["intent/model | jobs | $total | $/Mout | good% | $/good"]
    for model, a in sorted(agg.items(), key=lambda kv: -kv[1]["cost"]):
        permout = (a["cost"] / a["outtok"] * 1e6) if a["outtok"] else None
        good_rate = (a["good"] / a["labeled"]) if a["labeled"] else None
        per_good = (a["cost"] / a["good"]) if a["good"] else None
        lines.append(f"{a['provider']}:{model} | {a['jobs']} | ${a['cost']:.2f} | "
                     f"{('$%.2f' % permout) if permout else '—'} | "
                     f"{('%.0f%%' % (100*good_rate)) if good_rate is not None else '—'} | "
                     f"{('$%.4f' % per_good) if per_good else '—'}")
    return "\n".join(lines), len(agg)


def _est_line(mode, model, n, in_tok, out_tok, cost):
    print(f"  {mode:<8} {model:<22} {n:>5} call(s) · in~{in_tok:,} out≤{out_tok:,} -> ~${cost:.4f}")


# ─────────────────────────────── reconstruct (judge) ───────────────────────────────
def reconstruct(run=False, limit=None):
    """Judge unlabeled calls for quality. Estimate-only unless run=True. Returns the estimate dict."""
    judge = config.advisor_judge_model()
    rows = _judgeable()
    if limit:
        rows = rows[:limit]
    total_unlabeled = _unlabeled_count()
    print(f"reconstruct — quality judge = {judge} (Batch API), caged by intent {META}:*")
    print(f"  unlabeled calls: {total_unlabeled:,}   judgeable (have stored prompt+output): {len(rows):,}")
    if not rows:
        print("  → nothing to judge. Enable calls.store_prompts so future calls store snippets the "
              "judge can read (historical/backfilled rows have none). 0 spend.")
        return dict(requests=0, cost=0.0, model=judge)

    in_tok = sum(_count_tokens(_JUDGE_SYS + _judge_prompt(p, o), judge) for _, _, _, p, o in rows)
    out_tok = _JUDGE_OUT_CEILING * len(rows)
    cost = pricing.batch_cost(judge, in_tok, out_tok)
    print("  ESTIMATE (zero paid calls):")
    _est_line("batch", judge, len(rows), in_tok, out_tok, cost)
    print(f"  meta budget: ${config.meta_cap():.2f}/day · spent today ${_meta_spent():.4f}")
    if not run:
        print("  estimate-only. Re-run with --run to submit (gate enforces the meta cap).")
        return dict(requests=len(rows), in_tok=in_tok, out_tok=out_tok, cost=cost, model=judge)
    return _reconstruct_run(judge, rows)


def _reconstruct_run(judge, rows):
    prov = _provider(judge)
    print(f"  SUBMITTING judge batch ({prov}) under intent {META}:reconstruct …")
    with calls.context(intent=f"{META}:reconstruct"):
        if prov == "anthropic":
            import anthropic
            reqs = [{"custom_id": cid, "params": {"model": judge, "max_tokens": _JUDGE_OUT_CEILING,
                     "system": _JUDGE_SYS, "messages": [{"role": "user", "content": _judge_prompt(p, o)}]}}
                    for cid, _, _, p, o in rows]
            c = anthropic.Anthropic(api_key=config.api_key("ANTHROPIC_API_KEY"))
            b = c.messages.batches.create(requests=reqs)   # gate meters → meta cap; may raise SpendGateRefused
            print(f"  submitted Anthropic batch {b.id}. Poll, then `spendguard reconstruct-apply {b.id}`.")
            return dict(batch=b.id, provider=prov, requests=len(rows))
        else:
            import tempfile, os
            fd, path = tempfile.mkstemp(suffix=".jsonl", prefix="sg_judge_")
            with os.fdopen(fd, "w") as f:
                for cid, _, _, p, o in rows:
                    f.write(json.dumps({"custom_id": cid, "method": "POST", "url": "/v1/chat/completions",
                        "body": {"model": judge, "max_tokens": _JUDGE_OUT_CEILING,
                                 "messages": [{"role": "system", "content": _JUDGE_SYS},
                                              {"role": "user", "content": _judge_prompt(p, o)}]}}) + "\n")
            from .submit import guarded_submit
            bid = guarded_submit(path, model=judge, cap_dollars=config.meta_cap())
            print(f"  submitted OpenAI batch {bid}. Poll, then `spendguard reconstruct-apply {bid}`.")
            return dict(batch=bid, provider=prov, requests=len(rows))


def apply_verdicts(verdicts):
    """verdicts: {call_id: 'GOOD'|'BAD'} → label calls with source='judge' (conf 0.95)."""
    n = 0
    for cid, v in verdicts.items():
        calls.feedback(cid, ok=str(v).strip().upper().startswith("GOOD"), source="judge")
        n += 1
    return n


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
    print(f"  meta budget: ${config.meta_cap():.2f}/day · spent today ${_meta_spent():.4f}")
    if not run:
        print("  estimate-only. Re-run with --run to synthesize (gate enforces the meta cap).")
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
    for it in data if isinstance(data, list) else []:
        if not isinstance(it, dict) or not it.get("lesson"):
            continue
        iid = learn.add_insight(it.get("intent"), str(it["lesson"])[:500],
                                evidence=str(it.get("evidence", ""))[:500], source="mined",
                                confidence=float(it.get("confidence", 0.5)))
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
    print(f"  meta budget: ${config.meta_cap():.2f}/day · spent today ${_meta_spent():.4f}")
    if not run:
        print("  estimate-only. Re-run with --run for the recommendation (gate enforces the meta cap).")
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


# ─────────────────────────────── misc ───────────────────────────────
def _provider(model):
    from . import adapters
    try:
        return adapters.provider_for(model)
    except Exception:
        return "anthropic" if str(model).startswith("claude") else "openai"


def _meta_spent():
    from . import budget
    try:
        return budget.meta_spent_today()
    except Exception:
        return 0.0


def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(prog="spendguard")
    ap.add_argument("op", choices=["reconstruct", "mine", "optimize"])
    ap.add_argument("--intent")
    ap.add_argument("--plan", help="(optimize) the model you're about to use")
    ap.add_argument("--limit", type=int, help="(reconstruct) cap how many calls to judge")
    ap.add_argument("--run", action="store_true", help="actually spend (default: estimate only). Capped by caps.meta.")
    a = ap.parse_args(argv)
    if a.op == "reconstruct":
        reconstruct(run=a.run, limit=a.limit)
    elif a.op == "mine":
        mine(run=a.run, intent=a.intent)
    else:
        optimize(intent=a.intent, plan=a.plan, run=a.run)
    return 0

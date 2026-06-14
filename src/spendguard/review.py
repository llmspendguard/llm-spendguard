"""Layer 2 — practice REVIEW: was the LLM use good AND smart, not just what it cost.

Output-correctness (reconstruct) ≠ approach-quality. A correct answer produced wastefully — 1-item/req,
bloated prompt, the wrong model, uncapped max_tokens — is still wasted money. review assembles a CONTEXT
BUNDLE per (intent, model): the conversation frame (what the chat said about this work — the before/why
and after/outcome), the representative prompt, the token RATIO (the packing/cap tell), the cost shape,
and the reconstructed quality. The reasoner judges smart-vs-wasteful and emits CONDITIONAL, context-rich
insights — IF {task_class/regime/output_shape} THEN {action} BECAUSE {mechanism}, with a quality_basis —
so each learning is reusable and (when scrubbed) shareable. Caged (spendguard:review), estimate-first.
"""
import json
from . import calls, callio, learn, config, pricing
from .submit import _count_tokens

META = "spendguard"
_OUT = 1600
_SYS = (
    "You audit how a team USED LLMs — not just cost, but whether the approach was smart. For each "
    "(intent, model) bundle you get: cost/job, reconstructed quality (good%), the avg input/output token "
    "ratio, a representative prompt+output, and notes the team's chat made about this work. Judge what was "
    "smart, what was WASTEFUL (1-item/req when output is tiny, bloated prompt, over-powered model for the "
    "quality achieved, uncapped tokens), and the smarter alternative. Output STRICT JSON and nothing else: "
    'a list of AT MOST 8 objects {"intent": str|null, "task_class": "classification|extraction|generation|'
    'judging|embedding|reasoning", "regime": "bulk|interactive", "output_shape": "short-structured|short-text'
    '|long-form", "condition": "IF ...", "action": "THEN ...", "mechanism": "BECAUSE ...", "lesson": "one line", '
    '"quality_basis": "judged|used|unverified", "confidence": 0..1, "evidence": "the number/snippet behind it"}. '
    "Tie cost claims to quality — a cheaper model is only better if quality holds; say 'unverified' when good% "
    "is unknown. Keep lessons under 220 chars. No code fences.")


def _token_ratio(intent, model, n=8):
    with callio._lock:
        rows = callio._db().execute(
            "SELECT prompt, output, out_tok FROM call_io WHERE intent IS ? AND model=? LIMIT ?",
            (intent, model, n)).fetchall()
    if not rows:
        return None
    ins = [_count_tokens(p or "", model) for p, o, ot in rows]
    outs = [(ot or _count_tokens(o or "", model)) for p, o, ot in rows]
    return dict(avg_in=sum(ins) // len(ins), avg_out=max(1, sum(outs) // len(outs)),
                prompt=rows[0][0] or "", output=rows[0][1] or "")


def _conversation_for(intent):
    """conversation_event labels linked (comments_on) to this intent's runs — the before/after frame."""
    if not intent:
        return []
    with learn._lock:
        rows = learn._db().execute(
            "SELECT DISTINCT n.label FROM graph_nodes n JOIN graph_edges e ON n.id=e.src "
            "JOIN graph_nodes r ON e.dst=r.id WHERE e.rel='comments_on' AND r.type='run' "
            "AND json_extract(r.attrs,'$.intent')=? LIMIT 6", (intent,)).fetchall()
    return [r[0] for r in rows]


def _bundles(top=10):
    """Top (intent, model) combos by cost, each with cost + quality + token-ratio + a sample + chat notes."""
    rates = callio.good_rates()
    out = []
    for intent, model, jobs, cost, good, bad in sorted(calls.summary(), key=lambda r: -(r[3] or 0)):
        tr = _token_ratio(None if intent == "(none)" else intent, model)
        if not tr or not (tr["prompt"] or "").strip():   # need the prompt to assess approach (skips empty/anthropic)
            continue
        gr = rates.get((intent, model)) or {}
        out.append(dict(intent=intent, model=model, jobs=jobs, cost=cost or 0,
                        good=gr.get("good_rate"), judged=gr.get("judged", 0), sampled=gr.get("sampled", 0),
                        tr=tr, conv=_conversation_for(None if intent == "(none)" else intent)))
        if len(out) >= top:
            break
    return out


def _bundle_text(b):
    q = (f"good {100*b['good']:.0f}% (judged {b['judged']}/{b['sampled']})") if b["good"] is not None else "UNVERIFIED"
    lines = [f"## {b['intent']} / {b['model']}",
             f"cost: {b['jobs']} jobs, ${b['cost']:.2f} (${b['cost']/max(1,b['jobs']):.2f}/job)",
             f"quality: {q}",
             f"tokens: avg in ~{b['tr']['avg_in']}, out ~{b['tr']['avg_out']} per request",
             f"sample prompt: {b['tr']['prompt'][:240]}",
             f"sample output: {b['tr']['output'][:140]}"]
    if b["conv"]:
        lines.append("chat notes: " + " | ".join(c[:70] for c in b["conv"][:4]))
    return "\n".join(lines)


def review(run=False, top=10):
    model = config.advisor_model()
    bundles = _bundles(top)
    print(f"review — practice audit = {model} (realtime), caged by intent {META}:*")
    if not bundles:
        print("  → no call_io samples to review. Run `spendguard fetch-io` first. 0 spend.")
        return dict(requests=0, cost=0.0)
    body = "\n\n".join(_bundle_text(b) for b in bundles)
    # larger window: reason WITH accumulated, corroborated learnings — esp. quality findings — so a
    # cost-only recommendation (e.g. "use nano") can't contradict what we already proved (nano under-quality).
    prior = learn.insights(min_conf=0.7)
    known = "\n".join(f"- {lesson}" for _i, lesson, _s, _c, _e in prior[:12])
    constraints = (f"\n\nKNOWN PRIOR LEARNINGS — respect these; do NOT recommend anything they contradict "
                   f"(e.g. don't push a cheaper model if quality was already shown insufficient):\n{known}"
                   if known else "")
    prompt = f"Audit these {len(bundles)} (intent, model) usages:\n\n{body}{constraints}"
    in_tok = _count_tokens(_SYS + prompt, model)
    cost = pricing.realtime_cost(model, in_tok, _OUT)
    print(f"  {len(bundles)} usage bundles (cost+quality+token-ratio+sample+chat).  ESTIMATE (zero paid calls):")
    print(f"    realtime {model} 1 call · in~{in_tok:,} out≤{_OUT:,} -> ~${cost:.4f}")
    from . import budget
    print(f"  meta budget: ${config.meta_cap():.2f}/day · spent today ${budget.meta_spent_today():.4f}")
    if not run:
        print("  estimate-only. Re-run with --run for the audit (gate enforces the meta cap).")
        return dict(requests=1, in_tok=in_tok, out_tok=_OUT, cost=cost, model=model)

    from . import adapters
    with calls.context(intent=f"{META}:review"):
        r = adapters.call(model, prompt, max_tokens=_OUT, system=_SYS)
    if r["error"]:
        print(f"  ERROR: {r['error']}")
        return dict(error=r["error"])
    added = _persist(r["text"])
    print(f"  produced {added} approach-quality insight(s) → learn.insights. Cost ${r['cost']:.4f}.")
    return dict(insights=added, cost=r["cost"], model=model)


def _persist(text):
    from .advisor import _parse_insights
    data = _parse_insights(text)
    if data is None:
        learn.add_insight(None, text.strip()[:500], source="review", confidence=0.4)
        return 1
    added = 0
    for it in data if isinstance(data, list) else []:
        if not isinstance(it, dict) or not it.get("lesson"):
            continue
        ctx = {k: it.get(k) for k in ("task_class", "regime", "output_shape", "condition", "action",
                                      "mechanism", "quality_basis")}
        iid = learn.add_insight(it.get("intent"), str(it["lesson"])[:500],
                                evidence=str(it.get("evidence", ""))[:500], source="review",
                                confidence=float(it.get("confidence", 0.5)), ctx=ctx, status="candidate")
        learn.add_node("insight", str(it["lesson"])[:80],
                       attrs={"source": "review", "task_class": it.get("task_class")}, id=iid)
        if it.get("intent"):
            learn.add_edge(iid, it["intent"], "concerns")
        added += 1
    return added


def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(prog="spendguard review")
    ap.add_argument("--top", type=int, default=10, help="how many top-cost (intent,model) usages to audit")
    ap.add_argument("--run", action="store_true", help="actually spend (default: estimate). Capped by caps.meta.")
    a = ap.parse_args(argv)
    review(run=a.run, top=a.top)
    return 0

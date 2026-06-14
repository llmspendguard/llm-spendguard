"""spendguard brief — turn "this is what we need to do" into a confirm-or-correct PLAN.

You won't recite the six things that make cost-work go well; this PRE-FILLS them with defaults grounded
in YOUR history + the advisor's learnings, so it's one glance to confirm or correct (not an interrogation).
The six: intent · quality-bar+verification · scale · budget · output-format · test-vs-prod. It also pulls
the advisor's per-intent recommendation (cheapest config that held quality) and what to AVOID (denylist),
and points at the next command. Deterministic + free; `--llm` adds a caged reasoner pass to infer fields
from the task text when history is thin.

  spendguard brief --task "re-type the new RXNORM drug codes into baseconcepts"
"""
import re
import argparse


def _slug(task):
    return re.sub(r"[^a-z0-9]+", "_", (task or "").lower()).strip("_")[:40] or "task"


def _known_intents():
    from . import calls
    with calls._lock:
        return [r[0] for r in calls._db().execute(
            "SELECT DISTINCT intent FROM calls WHERE intent IS NOT NULL "
            "AND intent NOT LIKE 'spendguard:%'").fetchall()]


def _match_intent(task):
    """Best existing intent by word overlap with the task; else a new slug."""
    toks = set(re.findall(r"[a-z0-9]+", (task or "").lower()))
    best, score = None, 0
    for it in _known_intents():
        s = len(toks & set(re.findall(r"[a-z0-9]+", it.lower())))
        if s > score:
            best, score = it, s
    return best, (best is not None and score > 0)


def _defaults(intent, task):
    from . import calls, callio, learn, models, config
    rows = [r for r in calls.summary(intent)]                 # (intent, model, jobs, cost, good, bad)
    rates = callio.good_rates()
    # per-model cost + quality for this intent
    permodel = []
    for it, m, jobs, cost, good, bad in rows:
        gr = rates.get((it, m)) or {}
        bad_for = models.ineffective(m, intent)
        permodel.append(dict(model=m, jobs=jobs, cost=cost or 0,
                             per=(cost / jobs) if jobs else None, good=gr.get("good_rate"),
                             denied=bool(bad_for)))
    primary = max(permodel, key=lambda d: d["jobs"], default=None)
    # recommend: cheapest non-denylisted model with acceptable/known quality; else primary
    cands = [d for d in permodel if not d["denied"] and d["per"] is not None]
    rec = min(cands, key=lambda d: d["per"]) if cands else primary
    denylist = [d["model"] for d in permodel if d["denied"]]
    ins = learn.insights(intent=intent)[:3]
    sample = ""
    with callio._lock:
        r = callio._db().execute("SELECT output FROM call_io WHERE COALESCE(intent,'(none)')=? AND output!='' LIMIT 1",
                                 (intent,)).fetchone()
        if r:
            sample = (r[0] or "")[:90]
    hist_cost = sum(d["cost"] for d in permodel)
    qbar = "UNVERIFIED — set a golden set / judge (no quality labels for this intent yet)"
    if rec and rec.get("good") is not None:
        qbar = f"≥ {100*rec['good']:.0f}% match to the reference (historical for {rec['model']})"
    return dict(permodel=permodel, primary=primary, rec=rec, denylist=denylist, insights=ins,
                sample=sample, hist_cost=hist_cost, daily_cap=config.daily_cap())


def brief(task, intent=None, run_llm=False):
    matched, known = (intent, True) if intent else _match_intent(task)
    intent = matched or _slug(task)
    d = _defaults(intent, task) if known or intent else {}
    rec = d.get("rec")
    print(f'brief — "{task}"\n  intent: {intent}  ({"matched from history" if known else "NEW — no history yet"})\n')
    print("PROPOSED PLAN (confirm, or tell me what to change):")
    print(f"  1. intent         {intent}")
    print(f"  2. quality bar    {d.get('qbar', 'define how good is checked (golden set / judge)')}")
    print(f"  3. scale          {_scale_default(task, d)}")
    print(f"  4. budget         estimate-first; "
          f"{('historical ~$%.2f for this intent' % d['hist_cost']) if d.get('hist_cost') else 'set a ceiling'}"
          + (f"; daily cap ${d['daily_cap']:.0f}" if d.get("daily_cap") else ""))
    print(f"  5. output format  {d.get('sample') or 'specify the exact shape (terser = cheaper)'}")
    print(f"  6. test vs prod   TEST first (pilot + estimate), then promote the winner")
    print("\nADVISOR (per-intent history):")
    if rec:
        q = f"good {100*rec['good']:.0f}%" if rec.get("good") is not None else "quality UNVERIFIED"
        print(f"  recommend: {rec['model']}  (~${rec['per']:.4f}/job, {q})")
    if d.get("denylist"):
        print(f"  AVOID: {', '.join(d['denylist'])}  (proven ineffective for this intent)")
    for it, lesson, src, conf, _ev in d.get("insights", []):
        print(f"  [{conf:.2f}] {lesson[:90]}")
    print(f"\n  → next: spendguard experiment --intent {intent} --model <cheap> ...  then  spendguard promote ...")
    print("  (I'll proceed on these defaults unless you change a line.)")
    if run_llm:
        _llm_refine(task, intent)
    return dict(intent=intent, known=known, rec=rec)


def _scale_default(task, d):
    m = re.search(r"\b([\d,]{2,})\b", task or "")
    if m:
        return f"{m.group(1)} items (from your task)"
    if d.get("primary") and d["primary"].get("jobs"):
        return f"~{d['primary']['jobs']} jobs (historical for this intent)"
    return "how many items? (drives batch/packing/tier)"


def _llm_refine(task, intent):
    from . import calls, config
    META = "spendguard"
    try:
        from . import adapters
        sysmsg = ("Given a short task description, propose the 6 briefing fields (intent, quality_bar, "
                  "scale, budget, output_format, test_or_prod) as terse defaults. <120 words.")
        with calls.context(intent=f"{META}:brief"):
            r = adapters.call(config.advisor_model(), f"Task: {task}\nIntent: {intent}", max_tokens=300, system=sysmsg)
        if not r["error"]:
            print("\n  LLM-inferred defaults (caged):\n  " + (r["text"] or "").replace("\n", "\n  "))
    except Exception:
        pass


def main(argv=None):
    ap = argparse.ArgumentParser(prog="spendguard brief")
    ap.add_argument("--task", required=True, help="what you want to do, in a sentence")
    ap.add_argument("--intent", help="force an intent (else matched/proposed)")
    ap.add_argument("--llm", action="store_true", help="also infer fields with the caged reasoner (small spend)")
    a = ap.parse_args(argv)
    brief(a.task, intent=a.intent, run_llm=a.llm)
    return 0

"""effort_titration — learn the CHEAPEST reasoning effort that HOLDS quality, per (intent, model).

The reasoning-effort twin of the token-budget titration (adapters._call_guarded floors a reasoning model's OUTPUT;
this settles its EFFORT). For an intent it A/Bs the effort ladder (minimal→low→medium→high, restricted to what the
endpoint accepts) on a sample of the intent's REAL recorded prompts, SCORES each output 1-10 with the judge (graded,
not a brittle good/bad bit), and — via an AGENTIC verdict over the per-effort score table — records the cheapest
effort that holds as a per-(intent,model) fact (models.record_effort). That fact is auto-applied at the call
chokepoint whenever a caller uses this intent WITHOUT an explicit effort; until measured, the model's FAMILY FLOOR
stands (never force 'high' everywhere — measured, glm at minimal returned 0 findings while high found the bug, but
forcing high burns budget; effort is a property of (intent, model) only measurement settles).

WHY AGENTIC, NOT A THRESHOLD (the agentic-decisions doctrine). "Does low's 8.6 hold vs high's 8.9?" depends on the
MEANING of the gap and the sample size — a judgement, so an LLM makes it, over the graded scores the judge produced.
There is no hand-picked margin: the verdict weighs score gap, usable-rate, error-rate, and n itself, says whether an
effort holds, and whether it is CONFIDENT enough to stop. The judge (per output) and the verdict (over the table)
are the two agentic decisions; everything else is plumbing.

INCREMENTAL + RESUMABLE FOR LARGER CASES (chunk-never-single-shot). The A/B never single-shots a big sample: it
expands a chunk at a time, taking the agentic verdict after each chunk and only continuing while it is not yet
confident — learning from each chunk. Progress is CHECKPOINTED after EVERY measured prompt (per-effort cursor), so an
OOM / hard crash mid-chunk resumes EXACTLY where it stopped and never re-pays a measured prompt; a FAILED or OVERSIZED
prompt is recorded BY IDENTITY (its index, persisted) so the loss is durable and visible, never a silent shrink of
the sample. Every meta call is deadline-bounded and every prompt size-bounded so nothing can wedge or OOM the run;
the early-stop and the cap are logged. Estimate-first, meta-capped.
"""
import datetime
import json
import sys

from . import adapters, advisor, bakeoff, budget, calls, config, gate, models, pricing
from .submit import _count_tokens

_LADDER = ("minimal", "low", "medium", "high")   # default tiers to A/B; the send-side heal drops any an endpoint rejects
_PILOT = 4                 # prompts per effort in the first round (the smallest useful read)
_CHUNK = 4                 # prompts added per effort when the verdict is not yet confident
_MAX_SAMPLE = 20           # hard ceiling on prompts per effort — a titration is a measurement, not the workload
_SCORE_OUT = 40            # room for the graded verdict {score, usable}
_VERDICT_OUT = 400         # room for the effort verdict {effort, quality_score, confident, why}
_JUDGE_TIMEOUT_S = 60      # deadline on each judge/verdict meta call — a hung judge must never wedge the whole run
_JUDGE_CONF = 0.9          # confidence stamped on a titration quality label (an LLM judge on a fresh output)
_MAX_PROMPT_CHARS = 500_000   # explicit per-unit size ceiling: a pathological prompt is refused BY IDENTITY and never
#                               sent (bounds per-prompt memory here, above adapters' own input-window guard)

_SCORE_SYS = ("You rate how well an LLM OUTPUT answers its PROMPT for the task. Return ONLY JSON "
              '{"score": <integer 1-10>, "usable": <boolean>} — 1=useless, 10=excellent; usable=true iff it is a '
              "correct, usable result for the prompt. No prose.")
_SCORE_SCHEMA = {"type": "object", "additionalProperties": False, "required": ["score", "usable"],
                 "properties": {"score": {"type": "integer", "minimum": 1, "maximum": 10},
                                "usable": {"type": "boolean"}}}

_VERDICT_SYS = (
    "You choose the CHEAPEST reasoning effort that still HOLDS quality for a task, from A/B results. Lower effort on "
    "the ladder is cheaper. An effort HOLDS if its quality is not MEANINGFULLY worse than the best effort's. Decide "
    "that YOURSELF from the evidence — weigh the mean-score gap RELATIVE TO the sample size and the spread of "
    "scores, together with the usable-rate and the error-rate: a difference that could plausibly be sampling noise "
    "given how few and how consistent the samples are argues for the cheaper effort, while a difference that is "
    "large relative to that spread, or a usable-rate collapse, or frequent errors, is real and argues against "
    "dropping. Do NOT apply any fixed numeric cutoff — judge significance in context. Set confident=false when the "
    "samples are too few or too close to call, so more will be gathered. Return ONLY JSON "
    '{"effort": "<one of the tested efforts>", "quality_score": <integer 1-10 it holds at>, '
    '"confident": <boolean>, "why": "<one sentence>"}.')
_VERDICT_SCHEMA = {"type": "object", "additionalProperties": False,
                   "required": ["effort", "quality_score", "confident", "why"],
                   "properties": {"effort": {"type": "string"}, "quality_score": {"type": "integer", "minimum": 1, "maximum": 10},
                                  "confident": {"type": "boolean"}, "why": {"type": "string"}}}


# ── incremental checkpoint (chunk-never-single-shot: persist per-effort progress so a re-run RESUMES EXACTLY) ──
def _ckpt_db():
    db = budget._ledger_db()
    with budget._lock:
        db.execute("CREATE TABLE IF NOT EXISTS effort_titration(intent TEXT, model TEXT, effort TEXT, "
                   "n INTEGER, sum_score REAL, usable INTEGER, failed TEXT, cost REAL, consumed INTEGER, "
                   "updated TEXT, PRIMARY KEY(intent, model, effort))")
        db.commit()
    return db


def _new_acc():
    # `consumed` = prompts ATTEMPTED for this effort (the per-effort resume cursor); n = those that scored cleanly;
    # `failed` = the indices whose call errored / was oversized / the judge could not score — recorded by IDENTITY.
    return {"sum_score": 0.0, "usable": 0, "n": 0, "failed": [], "cost": 0.0, "consumed": 0}


def _load_progress(intent, model, efforts):
    """Seed the per-effort accumulators (incl. the resume cursor `consumed` and the failed-index list) from the
    checkpoint, so a re-run continues EXACTLY rather than re-paying. A fresh (intent, model, effort) loads zero."""
    db = _ckpt_db()
    acc = {}
    with budget._lock:
        for e in efforts:
            r = db.execute("SELECT n,sum_score,usable,failed,cost,consumed FROM effort_titration "
                           "WHERE intent=? AND model=? AND effort=?", (intent, model, e)).fetchone()
            if not r:
                acc[e] = _new_acc()
                continue
            try:
                failed = json.loads(r[3]) if r[3] else []
            except Exception:
                failed = []
            acc[e] = {"n": r[0] or 0, "sum_score": r[1] or 0.0, "usable": r[2] or 0, "failed": failed,
                      "cost": r[4] or 0.0, "consumed": r[5] or 0}
    return acc


def _save_one(intent, model, effort, a):
    """Persist ONE effort's accumulator — called after EVERY measured prompt, so a mid-chunk crash loses nothing
    already measured and a failed prompt stays recorded by identity (the durable checkpoint the resume reads)."""
    db = _ckpt_db()
    ts = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")
    with budget._lock:
        db.execute("INSERT OR REPLACE INTO effort_titration VALUES (?,?,?,?,?,?,?,?,?,?)",
                   (intent, model, effort, a["n"], a["sum_score"], a["usable"], json.dumps(a["failed"]),
                    a["cost"], a["consumed"], ts))
        db.commit()


def _score_output(prompt, output, judge_model):
    """Grade ONE (prompt, output) 1-10 with a usable flag — the graded quality signal (decided by the LLM judge,
    returned as typed fields, never parsed from prose). The FULL prompt+output are judged: a quality verdict on a
    truncated answer is a verdict on a different answer (sample prompts are already short from call_io, and the
    call's input guard bounds a pathological output). Deadline-bounded. None if the judge failed / gave no verdict."""
    r = adapters.call(judge_model, advisor._judge_prompt(prompt, output or ""), max_tokens=_SCORE_OUT,
                      system=_SCORE_SYS, schema=_SCORE_SCHEMA, sig="spendguard:effort-score", timeout_s=_JUDGE_TIMEOUT_S)
    if r.get("error") or not r.get("text"):
        return None
    try:
        j = r.get("json") if isinstance(r.get("json"), dict) else json.loads(r["text"])
        if isinstance(j, dict) and isinstance(j.get("score"), int) and isinstance(j.get("usable"), bool):
            return {"score": j["score"], "usable": j["usable"]}
    except Exception:
        return None
    return None


def _stats(acc):
    n = acc["n"]
    return {"n": n, "consumed": acc["consumed"], "mean_score": (acc["sum_score"] / n) if n else 0.0,
            "usable_rate": (acc["usable"] / n) if n else 0.0, "errors": len(acc["failed"]),
            "failed": list(acc["failed"]), "cost": acc["cost"]}


def _effort_verdict(intent, model, per_effort, judge_model):
    """The AGENTIC pick: cheapest effort that holds quality, over the per-effort score table. Returns
    {effort, quality_score, confident, why} or None (judge failed). The ONLY decision here — no threshold.
    Deadline-bounded, meta-caged."""
    order = {e: i for i, e in enumerate(models._EFFORT_LADDER)}
    rows = sorted((e for e in per_effort if per_effort[e]["consumed"]), key=lambda e: order.get(e, 99))
    if not rows:
        return None
    table = ["effort | n | mean_score | usable% | errors | $/call"]
    for e in rows:
        s = per_effort[e]
        table.append("%s | %d | %.1f | %.0f%% | %d | $%.5f"
                     % (e, s["n"], s["mean_score"], 100 * s["usable_rate"], s["errors"],
                        s["cost"] / s["n"] if s["n"] else 0.0))
    prompt = ("Task intent: %s\nModel: %s\nReasoning-effort A/B (cheapest first):\n%s\n\nPick the cheapest effort "
              "that holds quality." % (intent, model, "\n".join(table)))
    r = adapters.call(judge_model, prompt, max_tokens=_VERDICT_OUT, system=_VERDICT_SYS, schema=_VERDICT_SCHEMA,
                      sig="spendguard:effort-verdict", timeout_s=_JUDGE_TIMEOUT_S)
    if r.get("error") or not r.get("text"):
        return None
    try:
        j = r.get("json") if isinstance(r.get("json"), dict) else json.loads(r["text"])
        if isinstance(j, dict) and j.get("effort") in per_effort:
            return {"effort": j["effort"], "quality_score": int(j.get("quality_score") or 0),
                    "confident": bool(j.get("confident")), "why": str(j.get("why") or "")[:200]}
    except Exception:
        return None
    return None


def _measure_one(model, effort, prompt, idx, intent, judge_model, acc):
    """Measure ONE prompt (index `idx`) at ONE effort: advance the resume cursor FIRST (so a failed/hung prompt is
    not re-run on resume), size-guard it, run the effort, score it, fold into `acc`, and record the measured row. An
    OVERSIZED, FAILED, or unscoreable prompt is recorded BY IDENTITY in acc['failed'] (its index) — never silently
    dropped, so a fragile effort with fewer usable samples can't masquerade as the cheaper winner. Caller checkpoints."""
    acc["consumed"] += 1
    if len(prompt or "") > _MAX_PROMPT_CHARS:                    # per-unit size guard: never OOM/spend on a pathological
        acc["failed"].append(idx)                               # input — record by identity and move on (loud, bounded)
        return
    r = adapters.call(model, prompt, sig=intent, reasoning=effort, timeout_s=120)   # gated; the effort under test
    if r.get("error"):
        acc["failed"].append(idx)
        return
    cost = float(r.get("cost") or 0.0)
    acc["cost"] += cost
    sc = _score_output(prompt, r.get("text"), judge_model)
    q = None
    if sc:
        acc["sum_score"] += sc["score"]
        acc["usable"] += 1 if sc["usable"] else 0
        acc["n"] += 1
        q = "good" if sc["usable"] else "bad"
    else:
        acc["failed"].append(idx)
    calls.insert(adapters.provider_for(model), model.split(":", 1)[-1], "realtime", cost,
                 in_tok=int(r.get("in_tok") or 0), out_tok=int(r.get("out_tok") or 0), intent=intent,
                 quality=q, quality_conf=(_JUDGE_CONF if q else None), who="effort-titration", effort=effort)


def _pilot_estimate(intent, model, efforts, prompts, judge_model):
    """Zero-spend estimate: a PILOT chunk per effort, each output scored, plus one verdict call. Coarse-high (a
    budget guard). A deliberate stop (a spend refusal / bad bound) PROPAGATES — only an unpriced-model miss is
    swallowed (the estimate then excludes that model, surfaced elsewhere), never a refusal."""
    def _price(*a):
        try:
            return pricing.realtime_cost(*a)
        except Exception as _e:
            if isinstance(_e, gate.deliberate_stop_types()):
                raise
            return 0.0
    pilot = prompts[:_PILOT]
    in_toks = [_count_tokens(p, model) for p in pilot]
    total = 0.0
    for _eff in efforts:
        for it in in_toks:
            total += _price(model, it, 500)                            # ~output for a real answer
    n_score = len(efforts) * len(pilot)
    total += _price(judge_model, sum(_count_tokens(p, judge_model) for p in pilot) * len(efforts),
                    _SCORE_OUT * n_score)                              # scoring judge
    total += _price(judge_model, 400, _VERDICT_OUT)                    # one verdict call
    return round(total, 5), n_score


def _most_used_model(intent):
    """The model with the most recorded rows for this intent — its de-facto default, the cheapest to titrate."""
    with calls._lock:
        r = calls._calls_db().execute(
            "SELECT model, COUNT(*) FROM calls WHERE intent=? AND model IS NOT NULL "
            "GROUP BY model ORDER BY 2 DESC LIMIT 1", (intent,)).fetchone()
    if not r:
        return None
    m = r[0]
    return m if ":" in m else "%s:%s" % (adapters.provider_for(m), m)


def titrate(intent, model=None, efforts=None, sample_n=_MAX_SAMPLE, run=False, budget_usd=None, judge_model=None):
    """A/B the effort ladder for (intent, model), score each output, and (run=True) record the cheapest holding
    effort as the effort:<intent> fact. `model` defaults to the intent's most-used model (its de-facto default —
    the cheapest one to make honest); pass one to titrate a specific model. `efforts` overrides the ladder.
    ESTIMATE-FIRST: run=False returns the plan + $ estimate and spends nothing; run=True executes (refusing if the
    pilot estimate exceeds `budget_usd`). Incremental + RESUMABLE: expand a chunk at a time, agentic verdict after
    each, stop when confident, up to `sample_n`, checkpointing after EVERY measured prompt. Returns a dict."""
    _judge_pinned = judge_model is not None
    judge_model = judge_model or config.advisor_judge_model()
    model = model or _most_used_model(intent)
    if not model:
        return dict(intent=intent, error="no model to titrate — this intent has no recorded model; pass model=…")
    efforts = [str(e).strip() for e in (efforts or _LADDER) if e and str(e).strip()] or list(_LADDER)
    prompts = bakeoff._sample_prompts(intent, sample_n)
    if not prompts:
        return dict(intent=intent, model=model, error="no sample tasks — this intent has no recorded prompts to "
                    "replay. Run `spendguard fetch-io` or seed via the bakeoff first.")

    est, n_score = _pilot_estimate(intent, model, efforts, prompts, judge_model)
    if not run:
        return dict(intent=intent, model=model, efforts=efforts, sample_available=len(prompts), pilot=_PILOT,
                    estimate_usd=est, estimate_only=True,
                    note="estimate only (~$%.4f for the pilot + scoring + verdict); call with run=True to measure. "
                         "May expand past the pilot only while the verdict is not confident (up to %d/effort), "
                         "checkpointing every prompt so a re-run resumes exactly." % (est, min(sample_n, _MAX_SAMPLE)))
    if budget_usd is not None and est > float(budget_usd):
        return dict(intent=intent, model=model, estimate_usd=est, refused=True,
                    note="pilot estimate ~$%.4f exceeds budget_usd $%.4f — not run. Raise budget_usd or shrink the "
                         "ladder/sample." % (est, float(budget_usd)))

    cap = min(int(sample_n), _MAX_SAMPLE, len(prompts))
    acc = _load_progress(intent, model, efforts)                # RESUME from any prior per-prompt checkpoint
    resumed = max((acc[e]["consumed"] for e in efforts), default=0)
    if resumed:
        sys.stderr.write("[spendguard] effort-titration '%s'/%s: resuming from checkpoint (up to %d sample(s)/effort "
                         "already measured)\n" % (intent, model, resumed))
    verdict = None
    stopped_early = False
    target = _PILOT
    with calls.context(intent=intent):                          # the A/B runs are this intent's WORKLOAD (recorded)
        while True:
            for e in efforts:
                while acc[e]["consumed"] < min(target, cap):
                    idx = acc[e]["consumed"]
                    _measure_one(model, e, prompts[idx], idx, intent, judge_model, acc[e])
                    _save_one(intent, model, e, acc[e])         # CHECKPOINT after every prompt — exact resume
            per_effort = {e: _stats(acc[e]) for e in efforts}
            verdict = _effort_verdict(intent, model, per_effort, judge_model)   # agentic, over the graded scores
            reached = min(acc[e]["consumed"] for e in efforts)
            if verdict and verdict.get("confident"):
                stopped_early = True
                sys.stderr.write("[spendguard] effort-titration '%s'/%s: verdict CONFIDENT at %d sample(s)/effort → "
                                 "%s (stopping early)\n" % (intent, model, reached, verdict["effort"]))
                break
            if reached >= cap:
                break
            target = min(target + _CHUNK, cap)
    reached = min(acc[e]["consumed"] for e in efforts)
    if not stopped_early:                                       # no silent cap: say the sample was exhausted
        sys.stderr.write("[spendguard] effort-titration '%s'/%s: reached the %d-sample cap without a confident "
                         "verdict — recording the tentative pick\n" % (intent, model, reached))
    per_effort = {e: _stats(acc[e]) for e in efforts}
    if not verdict:
        return dict(intent=intent, model=model, per_effort=per_effort, sampled=reached,
                    note="no verdict (the judge produced no clean pick) — no effort fact recorded; family floor stands.")

    # Record the RECEIPT first (a deliberate stop — spend refusal / lock / deadline — halts HERE, before the fact is
    # applied; the paid A/B rows are already persisted, so nothing is lost and a re-run resumes). Then the fact,
    # which is idempotent (INSERT OR REPLACE on (model, key)) so a retry re-applies the same verdict, never a dup.
    from . import measurement
    import time as _time
    reading_id = None
    try:
        reading_id = measurement.record_reading(
            intent=intent, kind="effort-titration", judge_mix=[judge_model], judge_basis="configured",
            judge_pinned=_judge_pinned, sample_ids=[measurement.item_id(p) for p in prompts[:reached]],
            rubric={"system": _SCORE_SYS, "schema": _SCORE_SCHEMA, "verdict": _VERDICT_SYS},
            candidates=["%s@%s" % (model, e) for e in efforts],
            values={e: {"mean_score": per_effort[e]["mean_score"], "usable_rate": per_effort[e]["usable_rate"],
                        "n": per_effort[e]["n"], "errors": per_effort[e]["errors"]} for e in efforts},
            aggregation="mean_score", spend_usd=sum(per_effort[e]["cost"] for e in efforts), ts=_time.time())
    except Exception as _e:
        if isinstance(_e, gate.deliberate_stop_types()):
            raise                                                # a refusal/deadline HALTS — do not apply the fact
        sys.stderr.write("[spendguard] effort-titration: measurement receipt NOT recorded (%s) — the verdict "
                         "stands and is still applied\n" % type(_e).__name__)
    # ONLY a CONFIDENT verdict becomes the applied fact. A tentative pick (the sample stayed too close/thin to
    # settle) must NOT be recorded — applying an effort the agent said it had insufficient evidence to choose is
    # exactly the silent-wrong-default this measures away. The receipt above still captured the data; the family
    # floor stands until a re-run (with more samples) settles it.
    if not verdict.get("confident"):
        return dict(intent=intent, model=model, sampled=reached, per_effort=per_effort, reading_id=reading_id,
                    verdict=verdict, applied_fact=None,
                    note="verdict TENTATIVE (effort %s @ %d/10) — NOT recorded or applied; the sample stayed too "
                         "close/thin to settle. Family floor stands; raise --sample or re-run to gather more."
                         % (verdict["effort"], verdict["quality_score"]))
    models.record_effort(model, intent, verdict["effort"], confidence=0.9)
    return dict(intent=intent, model=model, sampled=reached, per_effort=per_effort, reading_id=reading_id,
                verdict=verdict, applied_fact="effort:%s = %s" % (intent, verdict["effort"]),
                note="recorded effort:%s = %s (quality %d/10, confident) — auto-applied when this intent is called "
                     "without an explicit effort; family floor until measured." % (intent, verdict["effort"],
                     verdict["quality_score"]))


def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(prog="spendguard effort-titrate",
                                 description="Learn the cheapest reasoning effort that holds quality per (intent, model).")
    ap.add_argument("intent")
    ap.add_argument("--model", help="model to titrate (default: the intent's most-used model)")
    ap.add_argument("--efforts", help="comma-separated ladder to A/B (default: minimal,low,medium,high)")
    ap.add_argument("--sample", type=int, default=_MAX_SAMPLE, help="max prompts per effort (default %d)" % _MAX_SAMPLE)
    ap.add_argument("--run", action="store_true", help="actually spend (default: estimate only)")
    ap.add_argument("--budget", type=float, help="refuse if the pilot estimate exceeds this")
    a = ap.parse_args(argv)
    r = titrate(a.intent, model=a.model, sample_n=a.sample, run=a.run, budget_usd=a.budget,
                efforts=[e for e in (a.efforts or "").split(",") if e.strip()] or None)
    print(json.dumps(r, indent=1, default=str))
    return 0 if not r.get("error") else 1

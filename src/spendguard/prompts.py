"""PROMPT-EFFICIENCY lint — mine the call corpus for waste, then hand each finding to the A/B lab.

The loop this powers (docs/PROMPT-EFFICIENCY.md):
    1. `spendguard prompts`         — LINT: findings below, ranked by measured $ at stake
    2. batch-1 of the same shape    — never scale an untested change (the gate enforces this)
    3. `spendguard experiment …`    — graduated A/B with the equivalence ladder (pluggable judges)
    4. promote-and-keep             — the winner becomes the recorded insight; the corpus re-verifies it

Findings are MEASURED from the calls table (opt-in corpus: `calls.enabled` + `calls.store_prompts` for
snippet-based checks), never guessed; prices come from pricing.py only. Each finding carries the exact
next command to run. Zero LLM spend — pure corpus analysis.
"""
import sqlite3

MIN_CALLS = 5          # fewer samples than this → not judged (law of small numbers)
LCP_MIN_CHARS = 60     # a shared prefix shorter than this isn't worth restructuring
SPREAD_RATIO = 3.0     # p95(in_tok) ≥ 3× p50 → context varies wildly (stuffing suspect)
SPREAD_MIN_TOK = 500   # …and the spread must be material in absolute tokens
UNDERBATCH_MIN_CALLS = 20   # a one-item-per-call pattern only wastes money at volume
UNDERBATCH_SMALL_TOK = 400  # a small median prompt → one item per call (pack many, or use the Batch API)
_BATCH_JUDGE_OUT = 120      # the packability verdict is one tiny JSON ({batchable, why}); a named cap, not a magic literal


def _batchable_verdict(intent, model, n, med_in):
    """AGENTIC: are these many small realtime calls PACKABLE batch-job items, or latency-sensitive interactive turns?
    The thresholds only LOCATED the candidate; whether it is a real batching opportunity is a MEANING judgement (two
    reasonable people can disagree — a bulk classify vs a chat turn), so a meta-caged LLM decides it, never a rule.
    Returns {batchable: bool, why: str}, or None when the judge is unavailable (→ the candidate is not emitted).
    A deliberate spend refusal (caps.meta) PROPAGATES — it halts the analysis, never degrades to a silent skip."""
    from . import adapters, calls, config, gate
    import json as _json
    q = (f"A job-type '{intent}' made {n} separate REALTIME LLM calls on {model}, each with a small (~{med_in}-token) "
         f"prompt. Are these INDEPENDENT items of one job that could be PACKED many-per-call or sent via the async "
         f"Batch API — or LATENCY-SENSITIVE interactive turns (a user waiting) that must stay realtime? "
         f'Return JSON only: {{"batchable": true|false, "why": "<one sentence>"}}.')
    try:
        with calls.context(intent="spendguard:batchable-judge"):           # meta-caged (caps.meta)
            r = adapters.call(config.advisor_judge_model(), q, sig="spendguard:batchable-judge",
                              max_tokens=_BATCH_JUDGE_OUT,
                              schema={"type": "object", "additionalProperties": False, "required": ["batchable"],
                                      "properties": {"batchable": {"type": "boolean"}, "why": {"type": "string"}}})
        j = r.get("json") if isinstance(r.get("json"), dict) else _json.loads(r.get("text") or "")
        return j if isinstance(j, dict) and isinstance(j.get("batchable"), bool) else None
    except gate.deliberate_stop_types():                                   # a spend refusal must HALT, not skip silently
        raise
    except Exception:
        return None


def _pctl(sorted_vals, p):
    if not sorted_vals:
        return 0
    i = min(len(sorted_vals) - 1, max(0, int(p * len(sorted_vals)) - (0 if p < 1 else 1)))
    return sorted_vals[i]


def _lcp(strings):
    """Longest common prefix across ≥2 strings."""
    if len(strings) < 2:
        return ""
    lo = min(strings)
    hi = max(strings)
    i = 0
    while i < len(lo) and lo[i] == hi[i]:
        i += 1
    return lo[:i]


def _in_price_per_tok(model):
    """$/input-token from pricing.py (never hardcoded); None when unpriced."""
    try:
        from . import pricing
        p = pricing.price(model)
        per_m = p.get("in_") if isinstance(p, dict) else None   # pricing.price -> {'in_','out','cached_in',...} $/1M
        return (float(per_m) / 1e6) if per_m else None
    except Exception as e:
        # "UNPRICED" AND "THE PRICE LOOKUP BROKE" ARE DIFFERENT FACTS and this collapsed them. Both return
        # None here because the caller can only do one thing about it, but the second one is a bug someone
        # needs to hear about rather than a model legitimately missing from the table.
        from . import config
        config.warn_once(f"[spendguard] price lookup FAILED for {model!r} ({type(e).__name__}) — this is a "
                         f"lookup error, not an unpriced model.")
        return None


def lint(intent=None, since=None, min_calls=MIN_CALLS, judge_batchable=False):
    """Findings, ranked by measured $ at stake. Each: {intent, kind, detail, est_usd, next}.

    The structural findings (boilerplate / context_spread / truncation / model_mix) are $0 — each fires on an
    OBJECTIVE fact whose remedy always applies. `batch_savings` is different: many small realtime calls only WASTE
    money if they are PACKABLE (independent items of one job), not if they are latency-sensitive interactive turns —
    and that is a MEANING judgement, not a threshold. So it is emitted ONLY under `judge_batchable=True`, which asks a
    small meta-caged LLM to rule packability per candidate (never a free $0 threshold-verdict). Default off → $0."""
    from . import config
    con = sqlite3.connect(config.db_path(), timeout=10)
    try:
        q = "SELECT intent, model, in_tok, out_tok, cost, finish, prompt_snip, kind FROM calls WHERE intent IS NOT NULL"
        args = []
        if intent:
            q += " AND intent = ?"; args.append(intent)
        if since:
            q += " AND ts >= ?"; args.append(since)
        try:
            rows = con.execute(q, args).fetchall()
        except sqlite3.OperationalError:
            rows = []          # `calls` table not created yet (first run / empty corpus) → no findings, not a crash
    finally:
        con.close()

    by_intent = {}
    for it, model, in_tok, out_tok, cost, finish, snip, kind in rows:
        by_intent.setdefault(it, []).append((model or "", in_tok or 0, out_tok or 0, cost or 0.0, finish or "", snip or "", kind or ""))

    findings = []
    _batch_cands = []          # (intent, model, rows, med_in, metered) → judged for packability after the loop (agentic)
    for it, rs in sorted(by_intent.items()):
        n = len(rs)
        if n < min_calls:
            continue
        total_cost = sum(r[3] for r in rs)
        models = {}
        for r in rs:
            models[r[0]] = models.get(r[0], 0) + 1
        dominant = max(models, key=models.get)

        # ── boilerplate: a long shared prefix re-sent on every call → cache / system-prompt / template it ──
        snips = [r[5] for r in rs if r[5]]
        if len(snips) >= min_calls:
            lcp = _lcp(snips)
            med_len = sorted(len(s) for s in snips)[len(snips) // 2]
            if len(lcp) >= LCP_MIN_CHARS and med_len and len(lcp) >= 0.5 * med_len:
                per_tok = _in_price_per_tok(dominant)
                lcp_tok = len(lcp) // 4                       # chars→tokens heuristic, only for the $ estimate
                est = round(lcp_tok * n * per_tok, 4) if per_tok else None
                findings.append({
                    "intent": it, "kind": "boilerplate", "est_usd": est,
                    "detail": (f"{n} calls share a {len(lcp)}-char prefix (~{lcp_tok} tok, ≥50% of the median prompt) "
                               f"— re-sent every call on {dominant}"),
                    "next": (f"move the shared prefix to a cached system prompt / packed-batch template, then "
                             f"A/B: spendguard experiment '{it}' --n 20"),
                })

        # ── context spread: wildly varying input sizes → likely context stuffing on the big ones ──
        ins = sorted(r[1] for r in rs if r[1] > 0)
        if len(ins) >= min_calls:
            p50, p95 = _pctl(ins, 0.50), _pctl(ins, 0.95)
            if p50 and p95 >= SPREAD_RATIO * p50 and (p95 - p50) >= SPREAD_MIN_TOK:
                per_tok = _in_price_per_tok(dominant)
                est = round((p95 - p50) * max(1, n // 10) * per_tok, 4) if per_tok else None
                findings.append({
                    "intent": it, "kind": "context_spread", "est_usd": est,
                    "detail": (f"input tokens p50={p50} vs p95={p95} ({p95 / max(p50, 1):.1f}×) across {n} calls — "
                               f"the largest calls likely stuff context the task doesn't need"),
                    "next": (f"trim retrieval/context on the top-decile calls, then verify equivalence: "
                             f"spendguard experiment '{it}' --n 20"),
                })

        # ── truncation: finish=length observed → max_tokens is BELOW what the task needs ──
        outs = sorted(r[2] for r in rs if r[2] > 0)
        truncs = sum(1 for r in rs if r[4] == "length")
        if truncs and outs:
            p99 = _pctl(outs, 0.99)
            findings.append({
                "intent": it, "kind": "truncation", "est_usd": round(total_cost * truncs / n, 4),
                "detail": f"{truncs}/{n} calls hit the max_tokens cap (finish=length) — truncated output wastes the whole call",
                "next": f"set max_tokens ≈ {int(p99 * 1.5)} (p99×1.5; cf. `spendguard maxtokens <sig>` for batch sigs), re-run batch-1",
            })

        # ── model mix: the same intent on multiple models → measured cascade candidate ──
        if len(models) >= 2:
            per_model = {}
            for m, in_t, out_t, cost, _f, _s, _k in rs:
                a = per_model.setdefault(m, [0, 0.0])
                a[0] += 1; a[1] += cost
            costs = {m: (c / max(k, 1)) for m, (k, c) in per_model.items()}
            cheap, dear = min(costs, key=costs.get), max(costs, key=costs.get)
            if costs[dear] > 0 and costs[cheap] < 0.5 * costs[dear]:
                est = round((costs[dear] - costs[cheap]) * per_model[dear][0], 4)
                findings.append({
                    "intent": it, "kind": "model_mix", "est_usd": est,
                    "detail": (f"runs on {len(models)} models; {cheap} averages ${costs[cheap]:.4f}/call vs "
                               f"{dear} ${costs[dear]:.4f} — a measured cascade candidate"),
                    "next": f"spendguard experiment '{it}' --models {cheap} --n 20   (equivalence ladder decides, not the price)",
                })

        # ── batching candidate (STRUCTURAL): many small REALTIME calls of one shape. Only COLLECTED here; whether it
        #    is a real batching opportunity (packable job items vs latency-sensitive interactive turns) is a MEANING
        #    judgement decided agentically after the loop — never by these thresholds, and only under judge_batchable.
        if judge_batchable:
            rt = [r for r in rs if r[6] == "realtime" and r[1] > 0]    # realtime kind, has an input token count
            if len(rt) >= UNDERBATCH_MIN_CALLS:
                med_in = sorted(r[1] for r in rt)[len(rt) // 2]
                metered = sum(r[3] for r in rt)                        # real $ paid ($0 rows were lane-served)
                if med_in <= UNDERBATCH_SMALL_TOK and metered > 0:
                    _batch_cands.append((it, dominant, rt, med_in, metered))

    # batch_savings — the PACKABILITY verdict is AGENTIC (one meta-caged LLM per candidate), never a threshold. A
    # candidate is emitted only when the model rules it PACKABLE; a latency-sensitive interactive workload is skipped.
    # EVERY candidate's outcome is COUNTED (emitted / not-packable / judge-unavailable) and the tally surfaced — a
    # dropped candidate is never silent.
    from . import pricing, gate, config as _cfg
    _considered, _emitted, _notpack, _unjudged = len(_batch_cands), 0, 0, 0
    for it, dominant, rt, med_in, metered in _batch_cands:
        v = _batchable_verdict(it, dominant, len(rt), med_in)
        if v is None:
            _unjudged += 1                                            # the judge was unavailable — traced, not dropped silently
            continue
        if not v.get("batchable"):
            _notpack += 1                                            # ruled NOT an opportunity (interactive / unpackable)
            continue
        save, n_unpriced = 0.0, 0
        for r in rt:
            if r[3] > 0:                                             # only a metered call has a batch saving
                try:
                    save += max(0.0, r[3] - pricing.batch_cost(r[0] or dominant, r[1], r[2]))
                except gate.deliberate_stop_types():                # a bad-bound / refusal must HALT, never be swallowed
                    raise
                except Exception:
                    n_unpriced += 1                                 # a genuinely unpriced model is COUNTED, not silent
        floor = f" (≥ floor: {n_unpriced} call(s) unpriced)" if n_unpriced else ""
        findings.append({
            "intent": it, "kind": "batch_savings", "est_usd": round(save, 4),
            "detail": (f"{len(rt)} small REALTIME calls (~{med_in}-tok median prompt) on {dominant} "
                       f"(${metered:.4f} metered) would cost ~${round(save, 4)} less at Batch-API rates — "
                       f"{(v.get('why') or '')[:120]}{floor}"),
            "next": "pack many items per call + submit via the Batch API; verify: spendguard experiment '%s' --n 20" % it,
        })
        _emitted += 1
    if _considered:                                                 # surface the tally so skipped candidates leave a trace
        _cfg.warn_once("[spendguard] prompts: batch_savings judged %d candidate(s) → %d packable, %d not-packable, "
                       "%d unjudged" % (_considered, _emitted, _notpack, _unjudged))

    findings.sort(key=lambda f: -(f["est_usd"] or 0))
    return findings


def main(argv=None):
    import sys, argparse, json as _json
    ap = argparse.ArgumentParser(prog="spendguard prompts",
                                 description="prompt-efficiency lint over the call corpus (zero spend); each finding carries its next A/B step")
    ap.add_argument("--intent", help="lint one intent only")
    ap.add_argument("--since", help="ISO date/ts lower bound")
    ap.add_argument("--min-calls", type=int, default=MIN_CALLS)
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    ap.add_argument("--judge-batchable", action="store_true",
                    help="agentically judge batching candidates (packable job items vs latency-sensitive interactive) "
                         "and emit `batch_savings` — SPENDS a small meta-caged call per candidate")
    a = ap.parse_args(sys.argv[2:] if argv is None else argv)
    fs = lint(intent=a.intent, since=a.since, min_calls=a.min_calls, judge_batchable=a.judge_batchable)
    if a.json:
        print(_json.dumps(fs, indent=1))
        return 0
    if not fs:
        print("prompts: no findings (corpus too small, or nothing above thresholds). "
              "Enable calls.enabled + calls.store_prompts to widen the lens.")
        return 0
    print(f"prompt-efficiency lint — {len(fs)} finding(s), ranked by measured $ at stake\n")
    for f in fs:
        est = f" (~${f['est_usd']:,.2f} at stake)" if f.get("est_usd") else ""
        print(f"  [{f['kind']}] {f['intent']}{est}\n      {f['detail']}\n      → {f['next']}\n")
    print("the loop: lint → batch-1 of the same shape → `spendguard experiment` (graded equivalence; "
          "plug your own judge via --mode custom:<module.fn>) → promote-and-keep → insight recorded")
    return 0

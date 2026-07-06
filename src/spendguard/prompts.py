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
    except Exception:
        return None


def lint(intent=None, since=None, min_calls=MIN_CALLS):
    """Findings, ranked by measured $ at stake. Each: {intent, kind, detail, est_usd, next}."""
    from . import config
    con = sqlite3.connect(config.db_path(), timeout=10)
    try:
        q = "SELECT intent, model, in_tok, out_tok, cost, finish, prompt_snip FROM calls WHERE intent IS NOT NULL"
        args = []
        if intent:
            q += " AND intent = ?"; args.append(intent)
        if since:
            q += " AND ts >= ?"; args.append(since)
        rows = con.execute(q, args).fetchall()
    finally:
        con.close()

    by_intent = {}
    for it, model, in_tok, out_tok, cost, finish, snip in rows:
        by_intent.setdefault(it, []).append((model or "", in_tok or 0, out_tok or 0, cost or 0.0, finish or "", snip or ""))

    findings = []
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
            for m, in_t, out_t, cost, _f, _s in rs:
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
    a = ap.parse_args(sys.argv[2:] if argv is None else argv)
    fs = lint(intent=a.intent, since=a.since, min_calls=a.min_calls)
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

"""Cross-check the ledger's recorded COST against the price of its recorded TOKENS — the guard that catches a
number 'quietly wrong' (tokens under-recorded relative to cost) BEFORE it is quoted as verified. It defends the
one column every other dollar figure rests on, costs nothing (pure arithmetic over the ledger), and runs in
`doctor` + a `receipt` line.

WHY A PROVABLE CEILING, NOT A 'SUSPICIOUS' THRESHOLD. The ledger stores no cache split, and prompt caching moves
cost in both directions (cache READS discount to a fraction; cache WRITES add a premium). So there is no honest
hand-picked band that separates "fine" from "wrong" — whether a 1.3x or 2x divergence is anomalous depends on the
model's cache behaviour, which is a judgement, not a cutoff. We therefore assert ONLY what is arithmetically
certain: the MOST the pricing model can charge for a set of tokens is every input token billed as a cache WRITE at
the largest write multiplier it defines (pricing.CACHE_WRITE_1H_MULTIPLIER) plus output at list. A recorded cost
ABOVE that ceiling cannot come from any honest pricing of the recorded tokens AT THE TABLE'S OWN RATES — so the
ledger's cost and token columns provably disagree: either the tokens are UNDER-RECORDED, or the price-table rate
for that model is STALE/too low. Both are real data-quality defects worth surfacing; WHICH it is is left to the
reader (the guard names the two causes, never asserts one). The disagreement itself is a fact two reasonable people
cannot dispute, so the flag is mechanical, not a meaning-decision. Costs BELOW the ceiling (cache reads, short
outputs, or a genuine under-capture) are indistinguishable without the cache split, so this guard stays SILENT on
them rather than guessing — storing the split (the follow-up) is what would let the below-ceiling direction be
checked too.

A symmetric |cost - list| check (what the hmmviz prompt proposed) fires on every cache-read-discounted day — it is
what misread a legitimate ~5x cache discount as a capture bug. This does not: it flags only the impossible.
"""
from . import config, pricing

_CEIL_EPS = 1e-6     # relative float-rounding tolerance comparing cost to the ceiling — NOT a 'suspicious' band


def _max_token_cost(model, in_tok, out_tok, batch):
    """The MAXIMUM cost the pricing model can assign to these tokens: every input token billed as a cache WRITE at
    the largest write multiplier the model defines (pricing.CACHE_WRITE_1H_MULTIPLIER), output at list. No honest
    call can cost more, so a recorded cost above this is a provable token UNDER-RECORDING — not a judgement. None
    when the model is unpriced (the bound is unknowable). Decomposed via the public cost fns (input-only + output-
    only) so it tracks their real rates for realtime vs batch without re-deriving the per-model price table."""
    fn = pricing.batch_cost if batch else pricing.realtime_cost
    try:
        in_only = fn(model, int(in_tok or 0), 0)
        out_only = fn(model, 0, int(out_tok or 0))
    except Exception:
        return None
    if in_only is None or out_only is None:
        return None
    return float(in_only) * float(pricing.CACHE_WRITE_1H_MULTIPLIER) + float(out_only)


def classify_impossible_cost_buckets(rows):
    """PURE verdict logic, split from the ledger read so it is testable without a database. `rows` = iterable of
    (model, day, kind, n, ledger_cost, in_tok, out_tok). Flags ONLY buckets whose recorded cost EXCEEDS the maximum
    the pricing model can assign to the recorded tokens (every input token as a 1h cache-write + output at list) —
    an arithmetically provable under-recording. Returns (findings_worst_first, unpriced_count); unpriced buckets are
    COUNTED (never silently treated as clean), consistent buckets are omitted."""
    findings, unpriced = [], 0
    for model, day, kind, n, ledger_cost, in_tok, out_tok in rows:
        ceiling = _max_token_cost(model, in_tok, out_tok, kind == "batch")
        if ceiling is None:
            unpriced += 1
            continue
        lc = float(ledger_cost or 0.0)
        if ceiling <= 0.0:
            if lc > 0.0:                 # billed a positive cost on ~zero priceable tokens → certain under-recording
                findings.append({"model": model, "day": day, "kind": kind, "n": int(n), "ledger_cost": lc,
                                 "ceiling": ceiling, "ratio": None})
            continue
        if lc > ceiling * (1.0 + _CEIL_EPS):
            findings.append({"model": model, "day": day, "kind": kind, "n": int(n), "ledger_cost": lc,
                             "ceiling": ceiling, "ratio": lc / ceiling})
    findings.sort(key=lambda f: -(f["ratio"] if f["ratio"] is not None else float("inf")))
    return findings, unpriced


def audit_metered_cost_vs_tokens(since_days=7):
    """Read the ledger and run classify_impossible_cost_buckets over metered PRICED rows (kind in realtime/batch,
    cost recorded, not suspect). Returns {ok, findings, unpriced, checked[, error]}. ok=False means the LEDGER READ
    ITSELF FAILED — a distinct, honest UNKNOWN that callers must NOT render as clean (a missing column or locked db
    is not 'no findings'). Read-only; own short-lived connection."""
    import contextlib
    import sqlite3
    try:
        since = f"-{int(since_days)} days"
        sql = ("SELECT model, date(ts) d, kind, COUNT(*) n, SUM(COALESCE(cost,0)) c, "
               "SUM(COALESCE(in_tok,0)) i, SUM(COALESCE(out_tok,0)) o FROM calls "
               "WHERE ts >= strftime('%s','now',?) AND kind IN ('realtime','batch') "
               "AND cost IS NOT NULL AND suspect IS NULL "
               "GROUP BY model, date(ts), kind")
        with contextlib.closing(sqlite3.connect(config.db_path())) as c:
            rows = c.execute(sql, (since,)).fetchall()
    except Exception as e:
        return {"ok": False, "error": str(e)[:200], "findings": [], "unpriced": 0, "checked": 0}
    findings, unpriced = classify_impossible_cost_buckets(rows)
    return {"ok": True, "findings": findings, "unpriced": unpriced, "checked": len(rows)}


def cost_integrity_lines(since_days=7):
    """doctor/receipt lines for the cost×token cross-check. A ledger READ FAILURE is UNKNOWN (never green). Each
    flagged bucket is a loud, PROVABLE under-recording line. A clean result states its COVERAGE (buckets checked /
    unpriced) so 'cannot tell' is never shown as 'clean'. PURE except for the read inside audit_metered_cost_vs_tokens."""
    r = audit_metered_cost_vs_tokens(since_days=since_days)
    if not r.get("ok"):
        return ["⚪ UNKNOWN — could NOT read the ledger to cross-check cost vs tokens (%s). Not the same as clean."
                % (r.get("error") or "read failed")]
    lines = []
    for f in r["findings"]:
        if f["ratio"] is None:
            lines.append(f"🔴 cost>max-token-price: {f['model']} {f['day']} ({f['kind']}, n={f['n']}) billed "
                         f"${f['ledger_cost']:.4f} on ~zero priceable recorded tokens — the cost and token columns "
                         f"disagree: tokens UNDER-RECORDED, or a missing/stale price. Investigate.")
        else:
            lines.append(f"🔴 cost>max-token-price: {f['model']} {f['day']} ({f['kind']}, n={f['n']}) billed "
                         f"${f['ledger_cost']:.4f} = {f['ratio']:.2f}x the MAX the price table can assign to its "
                         f"recorded tokens (every input token as a 1h cache-write + output at list; ceiling "
                         f"${f['ceiling']:.4f}) — above what any caching permits, so the ledger's own cost and token "
                         f"columns disagree: tokens UNDER-RECORDED, or a STALE price for this model. Investigate.")
    if not lines:
        cov = f"{r['checked']} metered bucket(s) checked"
        if r["unpriced"]:
            cov += f", {r['unpriced']} unpriced (not checkable — no price)"
        lines.append(f"🟢 no metered cost exceeds the max possible price of its recorded tokens — {cov} (last {since_days}d)")
    elif r["unpriced"]:
        lines.append(f"ℹ {r['unpriced']} unpriced metered bucket(s) could not be cross-checked (no price in the catalog)")
    return lines

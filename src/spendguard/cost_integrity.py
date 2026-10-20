"""Cross-check metered costs using disjoint input/output/cache-read/cache-write axes.

Split rows use catalog rates and pricing's five-minute cache-write convention. Legacy rows lack
cache evidence: retain the old ceiling as best effort, but report a named coverage gap.
"""
import logging
import math
import traceback

from . import ledger_read, pricing

COST_INTEGRITY_LOGGER = logging.getLogger(__name__)
COST_INTEGRITY_LEGACY_WIDTH = 7

_CEIL_EPS = 1e-6     # relative float-rounding tolerance comparing cost to the ceiling — NOT a 'suspicious' band


def _max_token_cost(model, in_tok, out_tok, batch):
    """The MAXIMUM cost the pricing model can assign to these tokens: every input token billed as a cache WRITE at
    the largest write multiplier the model defines (pricing.CACHE_WRITE_1H_MULTIPLIER), output at list. No honest
    call can cost more, so a recorded cost above this is a provable token UNDER-RECORDING — not a judgement. None
    when the model is unpriced (the bound is unknowable). Decomposed via the public cost fns (input-only + output-
    only) so it tracks their real rates for realtime vs batch without re-deriving the per-model price table."""
    fn = pricing.batch_cost if batch else pricing.realtime_cost
    in_only = fn(model, int(in_tok or 0), 0)
    out_only = fn(model, 0, int(out_tok or 0))
    if in_only is None or out_only is None:
        return None
    return float(in_only) * float(pricing.CACHE_WRITE_1H_MULTIPLIER) + float(out_only)


def _reconstruct_split_token_cost(model, in_tok, out_tok, cache_read_tok, cache_write_tok, batch):
    """Price disjoint token axes without pricing._cost's missing-cache-rate fallback.

    in_tok here is fresh input (as in spend_events), whereas pricing's public helpers include reads in input.
    Cache creation uses the same five-minute multiplier as pricing._cost; one-hour evidence needs its own axis.
    """
    counts = (in_tok, out_tok, cache_read_tok, cache_write_tok)
    if any(value is None or not math.isfinite(float(value)) or float(value) < 0 for value in counts):
        raise ValueError(f"Incomplete or invalid token axes: {counts!r}")
    rates = pricing.price(model)
    input_key, output_key = ("batch_in", "batch_out") if batch else ("in_", "out")
    read_key = "batch_cached_in" if batch else "cached_in"
    input_rate, output_rate = rates[input_key], rates[output_key]
    read_rate = rates[read_key] if cache_read_tok else 0
    if cache_write_tok and rates.get("provider") != "anthropic":
        raise ValueError(f"No authoritative cache-write rate for {model!r}: {rates!r}")
    write_rate = input_rate * pricing.CACHE_WRITE_5M_MULTIPLIER
    components = tuple(float(tokens) * float(rate) / pricing.ONE_MILLION
                       for tokens, rate in zip(counts, (input_rate, output_rate, read_rate, write_rate)))
    if not all(math.isfinite(component) and component >= 0 for component in components):
        raise ValueError(f"Invalid priced token components: {components!r}")
    return sum(components)


def classify_impossible_cost_buckets(rows, *, gaps=None):
    """Return mismatches and uncheckable count; optionally collect named coverage gaps.

    Seven-column rows are legacy ceiling-only evidence. Nine-column rows append disjoint cache read/write counts.
    Numeric comparison is accounting arithmetic, not a semantic judgement.
    """
    findings, unpriced = [], 0
    for row in rows:
        model, day, kind, n, ledger_cost, in_tok, out_tok, *cache_axes = row
        split = len(row) != COST_INTEGRITY_LEGACY_WIDTH and all(axis is not None for axis in cache_axes)
        identity = {"model": model, "day": day, "kind": kind, "n": int(n)}
        if not split and gaps is not None:
            gaps.append({**identity, "gap": "legacy_cache_split_missing", "basis": "best-effort ceiling only"})
        try:
            expected = (_reconstruct_split_token_cost(model, in_tok, out_tok, *cache_axes, kind == "batch")
                        if split else _max_token_cost(model, in_tok, out_tok, kind == "batch"))
            if expected is None:
                raise ValueError("Pricing returned no token cost")
            lc = float(ledger_cost)
            if not math.isfinite(lc) or lc < 0:
                raise ValueError(f"Invalid ledger cost: {ledger_cost!r}")
        except pricing.PriceUnknownError as exc:
            # Missing catalog data is incomplete coverage, retained as a named gap.
            unpriced += 1
            if gaps is not None:
                gaps.append({**identity, "gap": "token_cost_reconstruction_unavailable",
                             "error": repr(exc)})
            continue
        except Exception as exc:
            COST_INTEGRITY_LOGGER.exception("Cost integrity reconstruction failed for %r", identity)
            unpriced += 1
            if gaps is not None:
                gaps.append({**identity, "gap": "token_cost_reconstruction_unavailable",
                             "error": f"{exc!r}\n{traceback.format_exc()}"})
            continue
        mismatch = (not math.isclose(lc, expected, rel_tol=_CEIL_EPS) if split
                    else lc > expected * (1.0 + _CEIL_EPS))
        if mismatch:
            findings.append({**identity, "ledger_cost": lc, "ceiling": expected,
                             "expected_cost": expected, "basis": "split" if split else "legacy ceiling",
                             "ratio": lc / expected if expected else None})
    findings.sort(key=lambda f: -(f["ratio"] if f["ratio"] is not None else float("inf")))
    return findings, unpriced


def audit_metered_cost_vs_tokens(since_days=7):
    """Read the ledger and run classify_impossible_cost_buckets over metered PRICED rows (kind in realtime/batch,
    cost recorded, not suspect). Returns {ok, findings, unpriced, checked[, error]}. ok=False means the LEDGER READ
    ITSELF FAILED — a distinct, honest UNKNOWN that callers must NOT render as clean (a missing column or locked db
    is not 'no findings'). Read-only; own short-lived connection."""
    try:
        rows = ledger_read.read_cost_integrity_buckets(since_days)
    except Exception as e:
        COST_INTEGRITY_LOGGER.exception("Cost integrity ledger read failed")
        return {"ok": False, "error": f"{e!r}\n{traceback.format_exc()}", "findings": [], "unpriced": 0, "checked": 0}
    gaps = []
    findings, unpriced = classify_impossible_cost_buckets((row.values for row in rows), gaps=gaps)
    return {"ok": True, "findings": findings, "unpriced": unpriced, "checked": len(rows), "gaps": gaps}


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
        if f.get("basis") == "split":
            lines.append(f"🔴 cost≠split-token-price: {f['model']} {f['day']} ({f['kind']}, n={f['n']}) "
                         f"recorded ${f['ledger_cost']:.4f}, reconstructed ${f['expected_cost']:.4f} "
                         "from input + output + cache_read + cache_write catalog rates. Investigate.")
        elif f.get("basis") == "legacy ceiling":
            lines.append(f"⚪ UNKNOWN — legacy ceiling exceeded: {f['model']} {f['day']} ({f['kind']}, n={f['n']}); "
                         "cache split missing, so this best-effort bound cannot verify the recorded cost.")
        elif f["ratio"] is None:
            lines.append(f"🔴 cost>max-token-price: {f['model']} {f['day']} ({f['kind']}, n={f['n']}) billed "
                         f"${f['ledger_cost']:.4f} on ~zero priceable recorded tokens — the cost and token columns "
                         f"disagree: tokens UNDER-RECORDED, or a missing/stale price. Investigate.")
        else:
            lines.append(f"🔴 cost>max-token-price: {f['model']} {f['day']} ({f['kind']}, n={f['n']}) billed "
                         f"${f['ledger_cost']:.4f} = {f['ratio']:.2f}x the MAX the price table can assign to its "
                         f"recorded tokens (every input token as a 1h cache-write + output at list; ceiling "
                         f"${f['ceiling']:.4f}) — above what any caching permits, so the ledger's own cost and token "
                         f"columns disagree: tokens UNDER-RECORDED, or a STALE price for this model. Investigate.")
    if r.get("gaps"):
        lines.extend(f"⚪ UNKNOWN — {gap['gap']}: {gap['model']} {gap['day']} ({gap['kind']}, n={gap['n']}) "
                     + gap.get("error", gap.get("basis", "")) for gap in r["gaps"])
    if not lines:
        cov = f"{r['checked']} metered bucket(s) checked"
        if r["unpriced"]:
            cov += f", {r['unpriced']} unpriced (not checkable — no price)"
        lines.append(f"🟢 no metered cost mismatch (split reconstruction or legacy ceiling) — {cov} (last {since_days}d)")
    elif r["unpriced"]:
        lines.append(f"ℹ {r['unpriced']} uncheckable metered bucket(s): token-cost reconstruction unavailable (see named gaps)")
    return lines

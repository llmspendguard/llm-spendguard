"""REALIZED efficiency — measured before/after $ per call around each insight's adoption, no counterfactuals.

The loss-led doctrine's other half: "Spend Guarded" estimates what the mechanical layer AVOIDED
(cache/block/cascade — counterfactual, honestly labeled). This module measures what the LEARNING loop
actually DELIVERED: for every intent with an adopted insight, compare the intent's own $/call BEFORE the
adoption timestamp vs AFTER (same corpus that priced the calls). realized = (before − after) × after_calls.
Negative deltas are shown, not hidden — a "win" that regressed is exactly what the lifecycle must see.

Measured events sync into the EXISTING guarded pipeline as source="realized" (high confidence — it's a
before/after measurement, not an estimate), so the org dashboard's "≥ certain" floor includes them.
Sync is INCREMENTAL and idempotent: state in ~/.spendguard/realized_state.json tracks the calls already
counted per intent, so re-running never double-records. Zero LLM spend — pure corpus arithmetic.
"""
import sqlite3

MIN_EACH = 5   # need ≥5 calls on BOTH sides of the adoption point to claim a measurement


def _state_path():
    from . import config
    return config.state_path("realized")     # one naming rule for every adapter


def _load_state():
    """This adapter's persisted state, or its own empty shape. FOUR copies of this existed, each a bare
    `except: return {...}` — which is also how a TRUNCATED state file (from the non-atomic writes that used
    to sit opposite them) presented as "nothing saved yet" rather than as damage."""
    from . import config
    return config.load_state("realized", {})


def measure(intent=None, min_each=MIN_EACH):
    """[{intent, adopted_ts, before_rate, after_rate, delta_per_call, after_calls, realized_usd}] —
    adoption point = the EARLIEST non-refuted insight recorded for that intent."""
    from . import config
    # adoption point per intent = the EARLIEST non-refuted insight ts. insights_full() doesn't expose ts,
    # so read it straight from the insights table (same sqlite as the rest of the learning layer).
    adopted = {}
    lcon = sqlite3.connect(config.db_path(), timeout=10)
    try:
        q = ("SELECT intent, MIN(ts) FROM insights "
             "WHERE intent IS NOT NULL AND (status IS NULL OR status != 'refuted') ")
        args = []
        if intent:
            q += "AND intent = ? "; args.append(intent)
        for it, ts in lcon.execute(q + "GROUP BY intent", args).fetchall():
            if it and ts:
                adopted[it] = ts
    except sqlite3.OperationalError:
        return []                                   # no insights table yet → nothing adopted
    finally:
        lcon.close()

    if not adopted:
        return []
    con = sqlite3.connect(config.db_path(), timeout=10)
    try:
        rows = []
        for it, ts in sorted(adopted.items()):
            got = con.execute(
                "SELECT AVG(CASE WHEN ts < ? THEN cost END), SUM(ts < ?), "
                "       AVG(CASE WHEN ts >= ? THEN cost END), SUM(ts >= ?) "
                "FROM calls WHERE intent = ? AND cost > 0", (ts, ts, ts, ts, it)).fetchone()
            b_rate, b_n, a_rate, a_n = got[0], got[1] or 0, got[2], got[3] or 0
            if b_n < min_each or a_n < min_each or not b_rate or a_rate is None:
                continue
            delta = b_rate - a_rate
            rows.append({"intent": it, "adopted_ts": ts,
                         "before_rate": round(b_rate, 6), "after_rate": round(a_rate, 6),
                         "delta_per_call": round(delta, 6), "after_calls": int(a_n),
                         "realized_usd": round(delta * a_n, 4)})
        rows.sort(key=lambda r: -r["realized_usd"])
        return rows
    finally:
        con.close()


def sync_to_guarded(rows=None):
    """Record POSITIVE realized deltas into the guarded pipeline (source='realized'), incrementally:
    only calls not yet counted for an intent contribute, so re-runs never double-record. Returns
    {synced_usd, intents}. Regressions (negative delta) are reported by measure() but never 'saved'."""
    from . import guard
    rows = measure() if rows is None else rows
    state = _load_state()
    synced, intents = 0.0, 0
    for r in rows:
        if r["delta_per_call"] <= 0:
            continue
        st = state.get(r["intent"], {})
        # after_calls = COUNT(ts >= adopted_ts). The counted_calls watermark is only comparable to it when BOTH
        # were measured against the SAME adoption point; if adopted_ts moved (re-adoption / recompute) the old
        # count is over a different window, so subtracting it would OVER-credit (window widened). On a window
        # change, rebaseline the watermark and credit nothing this run — conservative; it never inflates savings.
        if "adopted_ts" in st and st["adopted_ts"] != r["adopted_ts"]:
            state[r["intent"]] = {"counted_calls": int(r["after_calls"]), "adopted_ts": r["adopted_ts"]}
            continue                                  # window MOVED (not a first sync) → rebaseline, credit nothing
        prev = int(st.get("counted_calls", 0))
        new_calls = r["after_calls"] - prev
        if new_calls <= 0:
            continue
        amount = round(r["delta_per_call"] * new_calls, 6)
        if amount <= 0:
            continue
        guard.record_saving("realized", amount, project=None)
        state[r["intent"]] = {"counted_calls": r["after_calls"], "adopted_ts": r["adopted_ts"]}
        synced += amount
        intents += 1
    # THE COMMENT THAT USED TO SIT HERE HAD IT BACKWARDS. It said a failed persist "must not take down a sync
    # that has already credited the savings above" — but the crediting having already happened is precisely
    # why the failure matters. record_saving() ran for each row; this write is what advances the watermark
    # that stops those same calls being credited AGAIN on the next run. Lose it and the sync is no longer
    # idempotent: every subsequent run re-credits the same work, and the savings figure climbs on its own
    # with nothing anywhere saying why. The write still does not raise — a sync that credited real savings
    # should return what it did — but the caller is TOLD, and the result says so.
    from . import config
    persisted = True
    try:
        if config.update_json(_state_path(), lambda _d: state) is None:
            persisted = False
    except Exception as e:
        persisted = False
        config.warn_once(f"[spendguard] realized-sync watermark write raised ({type(e).__name__})")
    if not persisted and intents:
        config.warn_once(
            f"[spendguard] realized sync credited ${synced:.4f} across {intents} intent(s) but could NOT "
            f"persist its watermark to {_state_path()} — the NEXT run will credit those same calls again. "
            f"Fix the write (disk space / permissions) before re-running, or the savings figure will inflate.")
    return {"synced_usd": round(synced, 4), "intents": intents, "persisted": persisted}


def main(argv=None):
    import sys, argparse, json as _json
    ap = argparse.ArgumentParser(prog="spendguard realized",
                                 description="measured before/after $ per call around each insight's adoption (no counterfactuals)")
    ap.add_argument("--intent", help="one intent only")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--sync", action="store_true", help="record NEW positive realized $ into the guarded pipeline (idempotent)")
    a = ap.parse_args(sys.argv[2:] if argv is None else argv)
    rows = measure(intent=a.intent)
    if a.json:
        print(_json.dumps(rows, indent=1))
    elif not rows:
        print("realized: no measurable intents yet (needs an adopted insight + ≥5 priced calls on each side)")
    else:
        total = sum(r["realized_usd"] for r in rows)
        print(f"realized efficiency — measured, not estimated   Σ ${total:,.2f}\n")
        for r in rows:
            tag = "regressed" if r["delta_per_call"] < 0 else "improved"
            print(f"  {r['intent']:<28} ${r['before_rate']:.4f}→${r['after_rate']:.4f}/call ({tag}) "
                  f"× {r['after_calls']} calls = ${r['realized_usd']:>9,.2f}   since {r['adopted_ts'][:10]}")
    if a.sync:
        res = sync_to_guarded(rows if not a.intent else None)
        print(f"  synced to guarded (source=realized): ${res['synced_usd']:,.2f} across {res['intents']} intent(s)")
    return 0

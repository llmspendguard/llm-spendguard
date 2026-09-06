"""measurement — receipts for a JUDGED number (bakeoff/eval good_rate): the MEASUREMENT twin of the spend receipt.

A *reading* is one judged number plus the full provenance needed to reproduce or compare it: the judge mix (the
models that ACTUALLY judged), the sample, the rubric. Each reading carries a citable `reading_id` and an
`instrument_id` = hash(intent + kind + judge_mix + aggregation + sample_hash + rubric_hash + candidates). Two
readings that share an `instrument_id` are COMPARABLE — the same ruler. See docs/MEASUREMENT_RECEIPTS.md.

Zero spend: `record_reading` STAMPS what a run already produced; `reconstruct` rebuilds a best-effort reading for a
PAST run from the `calls` corpus — and marks the judge `unknown` (it was never stamped before receipts existed),
never a guess. "Cannot tell" is not "clean."
"""
import hashlib
import json
import sqlite3
import time

from . import config


def _measurements_db():
    """The measurements store — a DEDICATED sqlite file, isolated from the money ledger (receipts are reconstructable
    from the corpus, so they need none of the ledger's backup/snapshot discipline)."""
    con = sqlite3.connect(str(config.HOME / "measurements.sqlite"))
    con.execute("""CREATE TABLE IF NOT EXISTS measurements(
        reading_id TEXT PRIMARY KEY, instrument_id TEXT, parent_id TEXT, ts REAL, intent TEXT, kind TEXT,
        judge_mix TEXT, judge_basis TEXT, judge_pinned INTEGER, aggregation TEXT, sample_ids TEXT, sample_hash TEXT,
        n INTEGER, rubric TEXT, rubric_hash TEXT, candidates TEXT, values_json TEXT, spend_usd REAL, sg_version TEXT)""")
    con.execute("CREATE INDEX IF NOT EXISTS idx_meas_instrument ON measurements(instrument_id)")
    con.execute("CREATE INDEX IF NOT EXISTS idx_meas_intent ON measurements(intent, ts)")
    con.commit()
    return con


def _meas_sha(*parts):
    h = hashlib.sha256()
    for p in parts:
        h.update(("\x00" + str(p)).encode("utf-8", "replace"))
    return h.hexdigest()


def item_id(text):
    """The canonical content id for ONE sample item (a prompt) — a stable hash, so the sample is pinned compactly and
    a rerun can re-match the SAME items from the corpus by id rather than by fragile position."""
    return _meas_sha(text)[:16]


def sample_hash(sample_ids):
    """Order-independent hash of the sample — so the SAME set of items keys the same, regardless of draw order."""
    return _meas_sha(*sorted(str(s) for s in (sample_ids or [])))[:24]


def rubric_hash(rubric):
    """Hash of the judging rubric (its prompt + schema). A changed rubric is a changed instrument, so it belongs in
    the identity — comparability breaks even on the same model when the question changes."""
    return _meas_sha(json.dumps(rubric, sort_keys=True, default=str))[:24]


def instrument_id(intent, kind, judge_mix, aggregation, s_hash, r_hash, candidates):
    """The RULER's identity: same value ⇒ two readings are comparable. Deliberately excludes the value and the date
    (those are the result, not the instrument). judge_mix + candidates are order-normalised."""
    return "in_" + _meas_sha(intent, kind, ",".join(sorted(judge_mix or [])), aggregation, s_hash, r_hash,
                        ",".join(sorted(candidates or [])))[:24]


def _sg_version():
    try:
        from importlib.metadata import version
        return version("llm-spendguard")
    except Exception:
        return "?"


def record_reading(*, intent, kind, judge_mix, sample_ids, rubric, candidates, values,
                   aggregation="single", judge_basis="served", judge_pinned=False, spend_usd=0.0,
                   parent_id=None, ts=None):
    """Persist ONE reading (a judged number + its provenance) and return its reading_id. Zero spend — it stamps a
    completed run. `judge_mix` = the judge model(s); `judge_basis` says what that list MEANS — 'served' (captured
    from what actually ran) vs 'configured' (the resolved config judge, which the bandit could have swapped unless
    pinned) — so a reader is never misled about whether it is what ran. `values` = {candidate: good_rate | {...}}.
    `ts` is stamped by the caller at call time."""
    ts = time.time() if ts is None else float(ts)
    jm = sorted(set(judge_mix or []))
    s_hash, r_hash = sample_hash(sample_ids), rubric_hash(rubric)
    iid = instrument_id(intent, kind, jm, aggregation, s_hash, r_hash, candidates or [])
    rid = "rd_" + _meas_sha(iid, ts, json.dumps(values, sort_keys=True, default=str))[:24]
    con = _measurements_db()
    con.execute("INSERT OR REPLACE INTO measurements VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (rid, iid, parent_id, ts, intent, kind, json.dumps(jm), judge_basis, 1 if judge_pinned else 0,
                 aggregation, json.dumps(list(sample_ids or [])), s_hash, len(sample_ids or []),
                 json.dumps(rubric, default=str), r_hash, json.dumps(sorted(candidates or [])),
                 json.dumps(values, default=str), float(spend_usd or 0.0), _sg_version()))
    con.commit()
    con.close()
    return rid


_COLS = ("reading_id", "instrument_id", "parent_id", "ts", "intent", "kind", "judge_mix", "judge_basis",
         "judge_pinned", "aggregation", "sample_ids", "sample_hash", "n", "rubric", "rubric_hash", "candidates",
         "values", "spend_usd", "sg_version")
_SELECT = ("SELECT reading_id,instrument_id,parent_id,ts,intent,kind,judge_mix,judge_basis,judge_pinned,aggregation,"
           "sample_ids,sample_hash,n,rubric,rubric_hash,candidates,values_json,spend_usd,sg_version FROM measurements")


def _row_to_reading(row):
    d = dict(zip(_COLS, row))
    for k in ("judge_mix", "sample_ids", "rubric", "candidates", "values"):
        try:
            d[k] = json.loads(d[k]) if d[k] is not None else None
        except (ValueError, TypeError):
            pass
    d["judge_pinned"] = bool(d.get("judge_pinned"))
    return d


def get_reading(reading_id):
    con = _measurements_db()
    try:
        row = con.execute(_SELECT + " WHERE reading_id=?", (reading_id,)).fetchone()
    finally:
        con.close()
    return _row_to_reading(row) if row else None


def list_readings(intent=None, limit=25):
    con = _measurements_db()
    try:
        q, args = _SELECT, ()
        if intent:
            q += " WHERE intent=?"
            args = (intent,)
        q += " ORDER BY ts DESC LIMIT ?"
        rows = con.execute(q, args + (int(limit),)).fetchall()
    finally:
        con.close()
    return [_row_to_reading(r) for r in rows]


def reconstruct_reading(intent):
    """Best-effort reading for a PAST bakeoff of `intent`, rebuilt from the `calls` corpus (caller='bakeoff'). Recovers
    candidates, good_rate, N and the date range — but the JUDGE is 'unknown': it was never stamped before receipts
    existed, and an unstamped judge is an absence of evidence, NEVER a guessed model. This is the RECONSTRUCT
    identity applied to a measurement; going forward, record_reading stamps the real judge so inspect shows it."""
    from . import calls
    con = calls._calls_db()
    # `who="bakeoff"` in calls.insert is stored in the `caller` column (see calls schema) — query by that.
    rows = con.execute(
        "SELECT model, quality, ts FROM calls WHERE caller='bakeoff' AND intent IS ? AND quality IS NOT NULL",
        (intent,)).fetchall()
    if not rows:
        return None
    per, tmin, tmax = {}, None, None
    for model, quality, ts in rows:
        d = per.setdefault(model, {"good": 0, "labeled": 0})
        d["labeled"] += 1
        if quality == "good":
            d["good"] += 1
        if ts is not None:
            tmin = ts if tmin is None else min(tmin, ts)
            tmax = ts if tmax is None else max(tmax, ts)
    values = {m: {"good": v["good"], "labeled": v["labeled"],
                  "good_rate": (v["good"] / v["labeled"] if v["labeled"] else None)} for m, v in per.items()}
    return {"intent": intent, "kind": "bakeoff", "reconstructed": True,
            "judge_mix": None, "judge_note": "unknown — not stamped before measurement receipts existed",
            "candidates": sorted(per), "values": values, "n_labeled": sum(v["labeled"] for v in per.values()),
            "date_range": [tmin, tmax]}


def inspect(reading_id):
    """A stored reading by id, or None. For a PAST run with no stored receipt, use reconstruct_reading(intent) —
    inspect never invents a judge for an unstamped number."""
    return get_reading(reading_id)


def _good_rate(reading, candidate):
    v = (reading.get("values") or {}).get(candidate)
    return v.get("good_rate") if isinstance(v, dict) else v


def compare(a_id, b_id):
    """$0 DRIFT-FLAG: are two readings COMPARABLE — same instrument_id (same judge + sample + rubric + candidates)?
    If so, the per-candidate delta; if not, WHY the ruler changed. So a time series is never silently continued
    across an instrument change: a 0.88→0.85 that is really 'different judge' is named, not read as a quality drop."""
    a, b = get_reading(a_id), get_reading(b_id)
    if not a or not b:
        return {"error": "one or both readings not found (%s, %s)" % (a_id, b_id)}
    comparable = a["instrument_id"] == b["instrument_id"]
    # `drift` = STRUCTURED codes for what changed (branchable by a caller/test); `reasons` = the human phrasing.
    drift, reasons = [], []
    if not comparable:
        if sorted(a["judge_mix"] or []) != sorted(b["judge_mix"] or []):
            drift.append("judge")
            reasons.append("judge %s → %s" % (a["judge_mix"], b["judge_mix"]))
        if a["rubric_hash"] != b["rubric_hash"]:
            drift.append("rubric")
            reasons.append("rubric changed")
        if a["sample_hash"] != b["sample_hash"]:
            drift.append("sample")
            reasons.append("sample changed")
        if sorted(a["candidates"] or []) != sorted(b["candidates"] or []):
            drift.append("candidates")
            reasons.append("candidates changed")
    delta = None
    if comparable:
        delta = {}
        for c in (a["candidates"] or []):
            ra, rb = _good_rate(a, c), _good_rate(b, c)
            delta[c] = {"from": ra, "to": rb,
                        "delta": (None if ra is None or rb is None else round(rb - ra, 4))}
    return {"comparable": comparable, "baseline": a_id, "candidate_reading": b_id, "drift": drift,
            "note": ("same instrument — comparable" if comparable
                     else "DIFFERENT instrument — NOT comparable to the baseline; this is a re-baseline, not a series point"),
            "reasons": reasons, "delta": delta,
            "caveat": ("a single rerun carries the judge's own noise — a small delta may not be significant; "
                       "rerun again for a spread" if comparable else None)}


def _recover_sample(intent, sample_ids):
    """Recover the ACTUAL prompts for a reading's sample (stored as item_id content-hashes) from the corpus, so a
    rerun replays the SAME items. Returns (prompts_in_order, missing_ids). A missing id = that item is no longer
    recorded → the sample cannot be fully reproduced, and the caller must re-baseline rather than pretend."""
    from . import callio
    # callio's OWN connection (the call_io table lives in config.db_path(), where record_io_sample writes) — never a
    # hand-rolled path, so a rerun reads exactly what the corpus stored. A real DB failure PROPAGATES; it is not
    # masked as 'nothing recovered' (which would mis-report a broken corpus as 'sample_unreproducible').
    con = callio._callio_db()
    by_id = {}
    for (p,) in con.execute("SELECT DISTINCT prompt FROM call_io WHERE intent IS ? AND prompt IS NOT NULL "
                            "AND length(prompt) > 0", (intent,)):
        by_id[item_id(p)] = p
    prompts, missing = [], []
    for sid in (sample_ids or []):
        (prompts.append(by_id[sid]) if sid in by_id else missing.append(sid))
    return prompts, missing


def rerun(reading_id, *, budget_usd=None, same_sample=True):
    """Re-run a reading's INSTRUMENT — same judge (pinned), same sample, same rubric — for a COMPARABLE number.
    ESTIMATE-FIRST: no budget_usd → returns the estimate only (0 spend); with it, bakeoff refuses over budget before
    spending. Records a CHILD reading (parent=reading_id) and reports the delta vs the baseline + whether the
    instrument reproduced. same_sample=False re-baselines on a FRESH sample with the same judge (tracks the
    population — a NEW instrument, flagged as such by compare). Rerun re-judges → it SPENDS."""
    r = get_reading(reading_id)
    if not r:
        return {"reason": "no_reading",
                "error": "no stored reading %r — for a past run try `measurement reconstruct <intent>`" % reading_id}
    if r.get("kind") != "bakeoff":
        return {"reason": "unsupported_kind",
                "error": "rerun currently supports bakeoff readings (this one is kind=%r)" % r.get("kind")}
    judge = (r.get("judge_mix") or [None])[0]
    prompts, missing = (None, [])
    if same_sample:
        prompts, missing = _recover_sample(r["intent"], r.get("sample_ids") or [])
        if missing:
            return {"reason": "sample_unreproducible", "baseline": reading_id,
                    "error": "cannot reproduce %d/%d sample items from the corpus (no longer recorded) — the SAME "
                    "sample is unavailable, so a comparable rerun is impossible. Use same_sample=False to re-baseline "
                    "on a fresh sample (same judge, a NEW instrument)." % (len(missing), len(r.get("sample_ids") or []))}
    from . import bakeoff
    res = bakeoff.bakeoff(r["intent"], candidates=r.get("candidates"), prompts=prompts, judge_model=judge,
                          parent_reading=reading_id, run=(budget_usd is not None), budget_usd=budget_usd)
    if budget_usd is None:
        return {"estimate": res, "baseline": reading_id, "pinned_judge": judge,
                "note": "estimate only — pass budget_usd to actually rerun this instrument (it re-judges → spends)."}
    if res.get("refused") or res.get("error"):
        return {"reason": "bakeoff_refused", "error": res.get("error") or res.get("note"), "baseline": reading_id}
    child = res.get("reading_id")
    return {"reading_id": child, "baseline": reading_id, "pinned_judge": judge,
            "comparison": (compare(reading_id, child) if child else None),
            "note": "rerun recorded — `measurement compare %s %s` shows the delta on the same instrument."
                    % (reading_id, child)}


def cmd(argv=None):
    """`spendguard measurement <inspect|list|reconstruct> …` — read the judge mix / sample / rubric behind a number."""
    import argparse
    ap = argparse.ArgumentParser(prog="spendguard measurement",
                                 description="Inspect the reproducible provenance of a judged number.")
    sub = ap.add_subparsers(dest="op", required=True)
    p_i = sub.add_parser("inspect", help="show a stored reading by id")
    p_i.add_argument("reading_id")
    p_l = sub.add_parser("list", help="list recent readings")
    p_l.add_argument("--intent", default=None)
    p_l.add_argument("--limit", type=int, default=25)
    p_r = sub.add_parser("reconstruct", help="best-effort reading for a PAST bakeoff intent (judge=unknown)")
    p_r.add_argument("intent")
    p_rr = sub.add_parser("rerun", help="re-run a reading's instrument (same judge/sample/rubric) for a comparable number")
    p_rr.add_argument("reading_id")
    p_rr.add_argument("--budget", type=float, default=None, help="spend up to this; OMIT for an estimate only (0 spend)")
    p_rr.add_argument("--fresh", action="store_true", help="re-baseline on a FRESH sample (same judge, a new instrument)")
    p_c = sub.add_parser("compare", help="drift-flag: are two readings comparable (same instrument)? show the delta")
    p_c.add_argument("baseline")
    p_c.add_argument("candidate")
    a = ap.parse_args(argv)
    if a.op == "inspect":
        r = inspect(a.reading_id)
        if not r:
            print(json.dumps({"error": f"no stored reading {a.reading_id!r} — for a past run try "
                              "`spendguard measurement reconstruct <intent>` (judge will be 'unknown')"}))
            return 1
        print(json.dumps(r, indent=1, default=str))
        return 0
    if a.op == "list":
        print(json.dumps(list_readings(intent=a.intent, limit=a.limit), indent=1, default=str))
        return 0
    if a.op == "reconstruct":
        r = reconstruct_reading(a.intent)
        print(json.dumps(r or {"error": f"no recorded bakeoff rows for intent {a.intent!r}"}, indent=1, default=str))
        return 0 if r else 1
    if a.op == "rerun":
        r = rerun(a.reading_id, budget_usd=a.budget, same_sample=not a.fresh)
        print(json.dumps(r, indent=1, default=str))
        return 1 if r.get("error") else 0
    if a.op == "compare":
        r = compare(a.baseline, a.candidate)
        print(json.dumps(r, indent=1, default=str))
        return 1 if r.get("error") else 0
    return 1

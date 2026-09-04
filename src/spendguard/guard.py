"""Quantify spend GUARDED (cache hits, blocked calls, cascade, advisor, plan-vs-API) as a DISTRIBUTION.

Each saving is an independent-ish random variable: a point estimate (`amount`) with a confidence-derived spread
(CV). We model each as LOGNORMAL (positive, right-skewed) and emit its cumulants; cumulants ADD over the
independent sum, so per (day, project, source) cumulant SUMS roll up to ANY scope on the server, which recovers
mean / median / std / skewness / excess-kurtosis + p10..p90. Saving events are recorded into the same SQLite
ledger as charges (a separate `savings` table). Sources beyond cache/block/cascade (advisor, plan-vs-API) call
record_saving() too — same pipe.
"""
import datetime
import math

from . import budget

# per-source confidence → coefficient of variation (lower confidence ⇒ wider spread). certain ⟂ counterfactual.
CONFIDENCE = {"cache": 0.95, "block": 0.70, "cascade": 0.90, "advisor": 0.50, "compaction": 0.65,
              "realized": 0.90}           # realized = MEASURED before/after per-call delta (realized.py), not a counterfactual
CERTAIN = ("cache", "block", "cascade", "realized")   # vs counterfactual: advisor, compaction

# EST-VALUE is the plan-served saving (work run $0 on a subscription plan instead of the metered API). It is ALREADY
# its own axis (est_chat_usd / lane_value / the receipt's est-value line), so it must NEVER also be booked here as a
# saving — that double-counts the same avoided dollars. `plan` is RESERVED for exactly that reason: record_saving
# refuses it, loudly, rather than silently inflating the tally. (The dominant "advisor/routing" saving IS this axis.)
_RESERVED_SOURCES = ("plan",)


def _savings_db():
    db = budget._ledger_db()                      # reuse the gate's SQLite file/connection
    with budget._lock:
        db.execute("CREATE TABLE IF NOT EXISTS savings "
                   "(ts TEXT, day TEXT, project TEXT, source TEXT, amount REAL, cv REAL)")
        db.execute("CREATE INDEX IF NOT EXISTS idx_savings_day ON savings(day)")
        db.commit()
    return db


def record_saving(source, amount, confidence=None, project=None):
    """Record one guarded-spend event (amount = $ that did NOT get spent because spendguard intervened).
    Never raises — guarding must not break the call path."""
    try:
        amount = float(amount or 0)
        if amount <= 0:
            return
        if source in _RESERVED_SOURCES:           # plan-served $ is the EST-VALUE axis — booking it here double-counts
            from . import config
            config.warn_once(f"[spendguard] record_saving({source!r}) REFUSED — plan-served savings are the est-value "
                             f"axis (est_chat_usd), not the savings ledger; booking them here double-counts the same "
                             f"avoided dollars. (guard._RESERVED_SOURCES)")
            return
        conf = CONFIDENCE.get(source, 0.6) if confidence is None else float(confidence)
        cv = max(0.05, min(0.9, 1.0 - conf))
        proj = project if project is not None else budget._project()
        now = datetime.datetime.now(datetime.timezone.utc)
        db = _savings_db()
        with budget._lock:
            db.execute("INSERT INTO savings (ts,day,project,source,amount,cv) VALUES (?,?,?,?,?,?)",
                       (now.isoformat(timespec="seconds"), now.strftime("%Y-%m-%d"), proj, source, amount, cv))
            db.commit()
    except Exception as e:
        # Never RAISES (guarding must not break the call path) — but never SILENT either: a swallowed DB write
        # loses a real saving with no trace, the same failure the budget dead-letter closes for spend.
        try:
            from . import config
            config.warn_once(f"[spendguard] record_saving({source}) write failed ({type(e).__name__}) — this "
                             f"saving was not recorded")
        except Exception:
            pass


def _lognormal_cumulants(mu, cv):
    """Cumulants k1..k4 of a lognormal with mean `mu` and CV `cv` (std = cv·mu). w = e^{σ_L²} = 1+cv²."""
    if mu <= 0:
        return (0.0, 0.0, 0.0, 0.0)
    w = 1.0 + cv * cv
    k1 = mu
    k2 = mu * mu * (w - 1.0)                       # variance
    if k2 <= 0:
        return (k1, 0.0, 0.0, 0.0)
    std = math.sqrt(k2)
    skew = (w + 2.0) * math.sqrt(w - 1.0)          # lognormal skewness
    exkurt = w**4 + 2 * w**3 + 3 * w**2 - 6        # lognormal excess kurtosis
    return (k1, k2, skew * std**3, exkurt * k2 * k2)


def by_dims_guarded(since=None):
    """Per (day, project, source): event count + SUMMED cumulants — the additive payload the server rolls up."""
    db = _savings_db()
    cond, args = [], []
    if since:
        cond.append("day >= ?"); args.append(since)
    where = ("WHERE " + " AND ".join(cond)) if cond else ""
    with budget._lock:
        rows = db.execute(f"SELECT day, COALESCE(project,''), source, amount, cv FROM savings {where}", args).fetchall()
    agg = {}
    for day, proj, source, amount, cv in rows:
        k1, k2, k3, k4 = _lognormal_cumulants(float(amount), float(cv if cv is not None else 0.3))
        a = agg.setdefault((day, proj, source),
                           {"day": day, "project": proj, "source": source, "n": 0, "k1": 0.0, "k2": 0.0, "k3": 0.0, "k4": 0.0})
        a["n"] += 1; a["k1"] += k1; a["k2"] += k2; a["k3"] += k3; a["k4"] += k4
    return list(agg.values())


def saved_since(since=None):
    """The guarded-savings running tally since `since`: {by_source, certain, counterfactual, total} in $ (mean).
    Mean = Σ of each event's lognormal k1 (the same additive cumulant the org rollup uses — one distribution, here
    collapsed to per-source means for a local readout). `certain` sums the MEASURED sources (CERTAIN); everything
    else is `counterfactual` — kept SEPARATE so the two are never blurred into one over-confident number. This is a
    THIRD axis (avoided $), never added into real-$ (billed) or est-value (plan)."""
    per = {}
    for r in by_dims_guarded(since=since):
        per[r["source"]] = per.get(r["source"], 0.0) + float(r["k1"])
    certain = round(sum(v for s, v in per.items() if s in CERTAIN), 4)
    counterfactual = round(sum(v for s, v in per.items() if s not in CERTAIN), 4)
    return {"by_source": {s: round(v, 4) for s, v in per.items()},
            "certain": certain, "counterfactual": counterfactual, "total": round(certain + counterfactual, 4)}


def savings_crosscheck(baseline_usd, since=None):
    """Cross-check the tally against ground truth WITHOUT a hand-picked verdict: return the FACTS — Σsaved, the
    (real-$ + est-value) baseline, their ratio, and the per-source DECOMPOSITION — and let the reader judge. There
    is no principled fixed cutoff for "too much saved": a single blocked batch can dwarf a low-spend month (21× is
    legitimate), so a magic-threshold 'plausible/implausible' flag would be exactly the hand-picked-number
    anti-pattern this repo forbids. Instead the number is made AUDITABLE — every dollar attributable to a named
    source, so a bogus source is VISIBLE rather than hidden inside a total. `ratio` is None when baseline is 0
    (unjudgeable). The un-regressable guard is decomposition: Σsaved == Σ(by_source)."""
    s = saved_since(since)
    base = float(baseline_usd or 0)
    ratio = (s["total"] / base) if base > 0 else None
    return {"saved": s["total"], "baseline": round(base, 4),
            "ratio": (round(ratio, 2) if ratio is not None else None),
            "by_source": s["by_source"], "certain": s["certain"], "counterfactual": s["counterfactual"]}

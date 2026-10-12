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
# best-value = the auto-selector chose a cheaper (model, effort) that held the intent's MEASURED quality bar; it is a
# counterfactual (the baseline call was never made), a little more grounded than a blind advisor swap since the pick
# is backed by recorded good_rate — hence a touch above 'advisor', still well below the CERTAIN (measured) sources.
CONFIDENCE = {"cache": 0.95, "block": 0.70, "cascade": 0.90, "advisor": 0.50, "compaction": 0.65,
              "realized": 0.90, "best-value": 0.55, "delegate": 0.55}   # delegate uses measured overage rate
CERTAIN = ("cache", "block", "cascade", "realized", "prompt_cache")   # vs counterfactual: advisor, compaction, best-value

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


# ── per-DECISION ledger (the value proof, and the learner's evidence, from one record) ──────────────────────
# The `savings` table above is the aggregate money axis (Σ avoided $ as a distribution). This table is its
# per-decision twin: one row per SUBSTITUTION — what the caller WOULD have run vs what spendguard chose, and the $
# delta at the tokens actually used. It makes the value PROVABLE (every saved dollar traces to intent +
# requested→chosen model+effort) rather than an anonymous aggregate; it is also exactly the record the learner reads.
# `why` and the bounded `reason_class` are additive columns after the 12 base columns. The read projection must keep
# `why` for local forensics, while only `reason_class` may feed the scrubbed aggregate sent off-device.
_DECISION_COLS = ("ts", "day", "project", "intent", "basis", "requested_model", "requested_effort",
                  "chosen_model", "chosen_effort", "counterfactual_usd", "actual_usd", "saved_usd", "why",
                  "reason_class")
_DECISION_REASON_CLASSES = frozenset({"paid-overage-admission", "proactive-shed", "quota-admission",
                                      "best-value", "substitution", "pinned", "other"})


def _decisions_db():
    db = budget._ledger_db()                       # same SQLite file as charges + savings
    with budget._lock:
        db.execute("CREATE TABLE IF NOT EXISTS decisions "
                   "(ts TEXT, day TEXT, project TEXT, intent TEXT, basis TEXT, "
                   "requested_model TEXT, requested_effort TEXT, chosen_model TEXT, chosen_effort TEXT, "
                   "counterfactual_usd REAL, actual_usd REAL, saved_usd REAL, why TEXT, reason_class TEXT)")
        # forward-only additive column: `why` = the SPECIFIC routing reason ("bandit → codex (gpt-5.6-sol)", "tier-3
        # base fallback", "lane-miss → metered", "best-value") so "why did X run instead of the model requested" is a
        # queryable fact, not only the coarse `basis` (advisor/best-value, which keys the savings tally).
        _have = {r[1] for r in db.execute("PRAGMA table_info(decisions)").fetchall()}
        if "why" not in _have:
            db.execute("ALTER TABLE decisions ADD COLUMN why TEXT")
        if "reason_class" not in _have:
            db.execute("ALTER TABLE decisions ADD COLUMN reason_class TEXT")
        db.execute("CREATE INDEX IF NOT EXISTS idx_decisions_day ON decisions(day)")
        db.execute("CREATE INDEX IF NOT EXISTS idx_decisions_intent ON decisions(intent)")
        db.commit()
    return db


def record_decision(intent, requested_model, chosen_model, counterfactual_usd, actual_usd, saved_usd,
                    requested_effort=None, chosen_effort=None, basis="advisor", project=None, why=None,
                    reason_class=None):
    """Record ONE routing decision (the value proof + the learner's evidence + the ROUTING PROVENANCE). `why` is the
    SPECIFIC reason the chosen model differs from the requested one (bandit/lane/tier-3/best-value/lane-miss → metered),
    so a cheap-intent-ran-on-an-expensive-model event is queryable and we can improve routing. Never raises — booking
    must not break the call path — but never SILENT: a failed write warns once rather than losing the row."""
    try:
        proj = project if project is not None else budget._project()
        now = datetime.datetime.now(datetime.timezone.utc)
        db = _decisions_db()
        with budget._lock:
            cls = reason_class if reason_class in _DECISION_REASON_CLASSES else None
            db.execute("INSERT INTO decisions (ts,day,project,intent,basis,requested_model,requested_effort,"
                       "chosen_model,chosen_effort,counterfactual_usd,actual_usd,saved_usd,why,reason_class) "
                       "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                       (now.isoformat(timespec="seconds"), now.strftime("%Y-%m-%d"), proj, intent, basis,
                        requested_model, requested_effort, chosen_model, chosen_effort,
                        float(counterfactual_usd or 0), float(actual_usd or 0), float(saved_usd or 0), why, cls))
            db.commit()
    except Exception:
        try:
            from . import config
            config.warn_once("[spendguard] record_decision write failed — a decision row was not recorded")
        except Exception:
            pass


def decisions_since(since=None):
    """Per-decision rows since `since` (day, YYYY-MM-DD), newest first — the forensic detail behind the tally."""
    db = _decisions_db()
    cond, args = [], []
    if since:
        cond.append("day >= ?"); args.append(since)
    where = ("WHERE " + " AND ".join(cond)) if cond else ""
    with budget._lock:
        rows = db.execute(f"SELECT {','.join(_DECISION_COLS)} FROM decisions {where} ORDER BY ts DESC", args).fetchall()
    return [dict(zip(_DECISION_COLS, r)) for r in rows]


def decisions_summary(since=None):
    """Aggregate the decision rows → {decisions, saved_usd, by_intent:[{intent, decisions, saved_usd}]} — the value
    proof, per intent. Σ(by_intent saved) == saved_usd is the un-regressable check that nothing is dropped."""
    rows = decisions_since(since)
    by_intent, total = {}, 0.0
    for d in rows:
        total += float(d.get("saved_usd") or 0)
        k = d.get("intent") or "(none)"
        b = by_intent.setdefault(k, {"intent": k, "decisions": 0, "saved_usd": 0.0})
        b["decisions"] += 1
        b["saved_usd"] += float(d.get("saved_usd") or 0)
    for b in by_intent.values():
        b["saved_usd"] = round(b["saved_usd"], 4)
    return {"decisions": len(rows), "saved_usd": round(total, 4),
            "by_intent": sorted(by_intent.values(), key=lambda x: x["saved_usd"], reverse=True)}


def _decision_reason_class(d):
    """Return only the bounded, structurally recorded routing class; never inspect free-form `why`."""
    if d.get("reason_class") in _DECISION_REASON_CLASSES:
        return d["reason_class"]
    if d.get("basis") == "best-value":
        return "best-value"
    requested, chosen = d.get("requested_model"), d.get("chosen_model")
    if requested and chosen and requested == chosen:
        return "pinned"
    if requested and chosen and requested != chosen:
        return "substitution"
    return "other"


def decision_rollup(since=None):
    """Bounded, scrubbed routing aggregates (by route+reason) for the org push — distinct from decisions_summary,
    which is the savings-by-intent receipt. No intent/project/time/free-form provenance leaves the client."""
    fields = ("requested_model", "requested_effort", "chosen_model", "chosen_effort")
    groups = {}
    for d in decisions_since(since):
        cls = _decision_reason_class(d)
        key = tuple(d.get(f) for f in fields) + (cls,)
        row = groups.setdefault(key, {**dict(zip(fields, key[:4])), "reason_class": cls, "n": 0,
                                      "billed_usd": 0.0, "counterfactual_usd": 0.0, "savings_usd": 0.0})
        row["n"] += 1
        row["billed_usd"] += float(d.get("actual_usd") or 0.0)
        row["counterfactual_usd"] += float(d.get("counterfactual_usd") or 0.0)
        row["savings_usd"] += float(d.get("saved_usd") or 0.0)
    ordered = sorted(groups.values(), key=lambda r: (-r["n"], str(tuple(r.get(f) for f in fields)),
                                                     r["reason_class"]))
    if len(ordered) > 24:
        kept, remainder = ordered[:23], ordered[23:]
        rollup = {**{f: None for f in fields}, "reason_class": "other", "n": sum(r["n"] for r in remainder),
                  "billed_usd": sum(r["billed_usd"] for r in remainder),
                  "counterfactual_usd": sum(r["counterfactual_usd"] for r in remainder),
                  "savings_usd": sum(r["savings_usd"] for r in remainder)}
        ordered = kept + [rollup]
    for row in ordered:
        for field in ("billed_usd", "counterfactual_usd", "savings_usd"):
            row[field] = round(row[field], 6)
    return {"n": sum(r["n"] for r in groups.values()), "by_route": ordered}


# ── shed EFFECTIVENESS (read-side) ───────────────────────────────────────────
# Every plan-admission shed already books a decision row (adapters._book_substitution: substituted_from = the lane's
# model the caller would have run, chosen = the ready alternative, counterfactual_usd = that baseline priced at the
# tokens ACTUALLY used, actual_usd = 0 when served free, why = the admission reason). This reads those rows and
# reports the shed — no new booking in the hot path (no double-count risk). The reason CLASS is parsed from the
# fixed-format string plan_admission._admission_base_reason emits (mechanical extraction of spendguard's own literal,
# not a meaning decision): 'paid-overage' = REAL money avoided (the plan would have billed overage); 'quota'/
# 'proactive' = quota PRESERVED (staying was within plan → $0, so NOT real money — a separate axis, never summed).
_SHED_CLASS = (("plan paid-overage admission", "overage"),
               ("proactive shed", "proactive"),
               ("plan quota admission", "quota"))


def _shed_class(why):
    """The admission class of a decision's `why`, or None if it is not a plan-admission shed (bandit/best-value/tier
    routing). Prefix-matches the literals _admission_base_reason emits — a fixed format, not a judgement."""
    w = str(why or "")
    for prefix, cls in _SHED_CLASS:
        if w.startswith(prefix):
            return cls
    return None


def shed_summary(since=None):
    """Shed effectiveness from the decision ledger: which lanes work was shed OFF, onto which READY lane, how much
    REAL overage $ that avoided, and how much quota it preserved — the proof the shed is working, per route.

    Returns {n_sheds, avoided_overage_usd, quota_preserved_usd, served_free_n, paid_n, by_route:[{from_lane, to_lane,
    class, n, served_free_n, counterfactual_usd}]}. avoided_overage_usd sums ONLY the 'overage' class served free (the
    one case shedding saves REAL money — a plan on paid overage would have billed); quota_preserved_usd sums the
    'quota'/'proactive' classes (est-value moved to another free lane, NOT real money) — kept on a SEPARATE axis so
    the two are never blurred (the same split the cost doctrine requires for billed vs est-value)."""
    from . import adapters
    def _lane_of(model):
        try:
            return adapters._LANES.get(adapters.provider_for(model), (None,))[0] or adapters.provider_for(model)
        except Exception:
            return model
    by_route, n, avoided, preserved, free_n, paid_n = {}, 0, 0.0, 0.0, 0, 0
    for d in decisions_since(since):
        cls = _shed_class(d.get("why"))
        if cls is None:
            continue
        n += 1
        cf = float(d.get("counterfactual_usd") or 0.0)
        served_free = float(d.get("actual_usd") or 0.0) <= 0.0
        free_n += 1 if served_free else 0
        paid_n += 0 if served_free else 1
        if cls == "overage" and served_free:
            avoided += cf
        elif cls in ("quota", "proactive"):
            preserved += cf
        key = (_lane_of(d.get("requested_model")), _lane_of(d.get("chosen_model")), cls)
        b = by_route.setdefault(key, {"from_lane": key[0], "to_lane": key[1], "class": cls,
                                      "n": 0, "served_free_n": 0, "counterfactual_usd": 0.0})
        b["n"] += 1
        b["served_free_n"] += 1 if served_free else 0
        b["counterfactual_usd"] += cf
    for b in by_route.values():
        b["counterfactual_usd"] = round(b["counterfactual_usd"], 6)
    return {"n_sheds": n, "avoided_overage_usd": round(avoided, 6), "quota_preserved_usd": round(preserved, 6),
            "served_free_n": free_n, "paid_n": paid_n,
            "by_route": sorted(by_route.values(), key=lambda x: x["counterfactual_usd"], reverse=True)}

"""Efficiency SIGNAL — the scrubbed, server-bound roll-up that turns the local corpus into "which work is worth
the spend." Per (project · intent · model): cost (from batch billing) + quality (good-rate from judging) + waste
(cost on bad outputs) + a short context label + a recommendation. NO raw prompts leave — only the signal.

Build: `spendguard signal` (preview) / `spendguard signal push` (→ /v1/signal, this repo's project, via its key).
"""
import datetime


def build(since=None):
    """Per (project, intent, model) signal rows from the recovered call corpus + batch costs + quality."""
    from . import callio, backfill, conv
    since = since or datetime.date.today().replace(day=1).isoformat()
    bcost = {}
    for _prov, model, cost, _it, _ot, day, bid in (backfill._openai_rows() + backfill._anthropic_rows()):
        if (day or "") >= since:
            bcost[bid] = bcost.get(bid, 0.0) + cost
    db = callio._db()
    rows = db.execute("SELECT COALESCE(NULLIF(intent,''),''), model, batch, COUNT(*), "
                      "SUM(quality IS NOT NULL), SUM(quality='good'), SUM(COALESCE(in_tok,0)), SUM(COALESCE(out_tok,0)) "
                      "FROM call_io GROUP BY intent, model, batch").fetchall()
    agg = {}
    for intent, model, batch, n, judged, good, itok, otok in rows:
        proj = conv._project_of(intent) or conv._project_of(model) or ""
        key = (proj, intent, model)
        a = agg.setdefault(key, dict(project=proj, intent=intent, model=model, calls=0, cost=0.0,
                                     judged=0, good=0, tin=0, tout=0, batches=set()))
        a["calls"] += n; a["judged"] += (judged or 0); a["good"] += (good or 0)
        a["tin"] += (itok or 0); a["tout"] += (otok or 0); a["batches"].add(batch)
    # cost is per BATCH (full billed), counted once per (intent,model) group that owns the batch
    for a in agg.values():
        a["cost"] = round(sum(bcost.get(b, 0.0) for b in a["batches"]), 6)

    def recommend(a, gr):
        if gr is not None and gr < 0.7:
            return f"low good-rate ({gr:.0%}) — review the prompt/model for this intent before scaling"
        if "opus" in (a["model"] or "").lower() and a["cost"] > 20:
            return "opus-heavy spend — A/B a cheaper model (spendguard experiment) on a fixed sample"
        if a["tin"] > 0 and a["calls"] and (a["tin"] / a["calls"]) < 40:   # only when we actually have token data
            return "tiny prompts — pack more per request (under-batching wastes flagship calls)"
        return ""

    day = datetime.date.today().isoformat()
    out = []
    for a in agg.values():
        gr = (a["good"] / a["judged"]) if a["judged"] else None
        out.append(dict(
            project=a["project"], intent=a["intent"], model=a["model"], day=day,
            calls=a["calls"], cost_micros=round(a["cost"] * 1_000_000),
            judged=a["judged"], good_rate=gr,
            waste_micros=round(a["cost"] * (1 - gr) * 1_000_000) if gr is not None else 0,
            tokens_in=a["tin"], tokens_out=a["tout"],
            context=(a["intent"] or "(none)")[:120], recommendation=recommend(a, gr),
        ))
    out += cancellation_rows()                # cancelled-but-billed = loss, surfaced as waste
    return out


def cancellation_rows():
    """Cancelled-but-billed batches = LOSS — partial work billed then discarded (your protocol: 'never cancel as
    cost control; completed requests still bill'). Surfaced as a signal row per project: full billed cost = waste,
    with the recommendation. Attributed by conversation/intent evidence. Free (provider GETs)."""
    import datetime
    from . import conv, callio, pricing
    try:
        from .reconcile_openai import load_key, fetch_batches
        batches = list(fetch_batches(load_key()))
    except Exception:
        return []
    links = conv.batch_links()
    try:
        b2i = {b: i for b, i in callio._db().execute("SELECT batch, COALESCE(NULLIF(intent,''),'') FROM call_io GROUP BY batch")}
    except Exception:
        b2i = {}
    by_proj = {}
    for b in batches:
        if b.get("status") != "cancelled":
            continue
        u = b.get("usage") or {}
        it, ot = u.get("input_tokens", 0), u.get("output_tokens", 0)
        if not (it or ot):
            continue                                            # nothing completed → genuinely $0
        bid = b["id"]
        cost = pricing.batch_cost(b["model"], it, ot, (u.get("input_tokens_details") or {}).get("cached_tokens", 0))
        proj = (conv._project_of(links[bid]["snippet"]) if bid in links else "") or \
               (conv._project_of(b2i[bid]) if b2i.get(bid) else "") or "unattributed"
        a = by_proj.setdefault(proj, {"cost": 0.0, "n": 0})
        a["cost"] += cost; a["n"] += 1
    day = datetime.date.today().isoformat()
    return [dict(project=proj, intent="cancelled-batches", model="(various)", day=day,
                 calls=a["n"], cost_micros=round(a["cost"] * 1_000_000), judged=0, good_rate=0.0,
                 waste_micros=round(a["cost"] * 1_000_000), tokens_in=0, tokens_out=0,
                 context="cancelled mid-run (partial work billed)",
                 recommendation=f"{a['n']} batches cancelled but still billed ${a['cost']:.0f} — completed requests bill; let jobs finish or estimate first, never cancel as cost control")
            for proj, a in by_proj.items()]


def push(dry=False):
    """Push THIS repo's project signal (scrubbed) → /v1/signal via the repo key. Same project filter as the
    ledger roll-up: the connection's own project(s), plus 'unattributed'/'llmseg' iff it owns_account — so the
    account-owner (lmm) also carries the shared no-evidence cancellation loss, and other repos don't double-count."""
    from . import saas
    c = saas.conn()
    flt = saas._project_filter(c)
    rows = [r for r in build() if flt is None or (r.get("project") or "") in flt]
    payload = {"signal": rows}
    if dry:
        return payload
    ok, reason = saas.ready()
    if not ok:
        return {"skipped": f"not connected: {reason}"}
    if c.get("visibility", "private") == "private":
        return {"skipped": "visibility=private"}
    return saas._request("POST", "/v1/signal", payload)


def cmd(argv=None):
    argv = argv or []
    if argv and argv[0] == "push":
        print("signal push:", push(dry="--dry" in argv))
        return 0
    rows = sorted(build(), key=lambda r: -r["cost_micros"])
    print("efficiency signal (per project · intent · model):")
    print(f"  {'project':12}{'intent':22}{'model':18}{'cost':>10}{'good':>7}{'waste':>9}  recommendation")
    for r in rows[:15]:
        gr = f"{r['good_rate']:.0%}" if r["good_rate"] is not None else "—"
        print(f"  {(r['project'] or '-')[:11]:12}{(r['intent'] or '-')[:21]:22}{r['model'][:17]:18}"
              f"${r['cost_micros']/1e6:>8.2f}{gr:>7}${r['waste_micros']/1e6:>8.2f}  {r['recommendation'][:50]}")
    return 0

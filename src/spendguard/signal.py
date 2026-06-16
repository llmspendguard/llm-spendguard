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
    return out


def push(dry=False):
    """Push THIS repo's project signal (scrubbed) → /v1/signal via the repo key."""
    from . import saas, budget
    c = saas.conn()
    proj = (c.get("project") or budget._project() or "").lower()
    rows = [r for r in build() if (r.get("project") or "") == proj]
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

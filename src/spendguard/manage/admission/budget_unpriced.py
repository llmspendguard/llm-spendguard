"""Own the UNPRICED spend write lifecycle: mark a call whose tokens are real but whose dollars are unknown with ``record_unpriced`` and ``cost_basis='unpriced'``, then retro-price those rows once a rate resolves with ``reprice_unpriced`` through the ledger's audited update, adjust, or skip correction path; unlike ``budget_unpriced_sink``, the dependency-free injection bridge that forwards a cyclic adapter's unpriced record into budget's wired recorder, this module reaches budget's engine core through the ``admission_accessors`` resolver seam and never imports budget."""

import datetime

from . import admission_accessors as _admission_accessors
from .admission_accessors import BASIS_ESTIMATE, BASIS_UNPRICED


def record_unpriced(provider, model, kind, in_tok=0, out_tok=0, project=None):
    """Record that a call HAPPENED but could not be priced. cost=0 because we refuse to invent a number, but
    the row is MARKED so no total treats it as 'free' and the receipt can name the model to price."""
    now = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")
    proj = (project if project is not None else _admission_accessors.resolve__project()) or ""
    # a $0 forensic marker (cost_basis='unpriced'), carrying the real tokens the price is unknown for.
    _admission_accessors.resolve__record_spend_event(
        provider, model, kind, 0, basis=BASIS_UNPRICED, project=proj,
        occurred_at=now, in_tok=in_tok, out_tok=out_tok, source="gate", dedup_suffix=":unpriced")


def reprice_unpriced(model=None, apply=False, actor="reprice"):
    """Retroactively PRICE 'unpriced' spend_events rows that are NOW priceable (a rate resolves for their exact
    (provider, model)). The tokens were always real; only the $ was unknown at gate time — the model had no price
    yet, or its provider was ambiguous. This gives each its catalog-rate cost through the ledger's OWN correction
    path: update() revises an OPEN row in place (cost_basis 'unpriced'→'estimate', sets the money column + rates,
    so it drops out of the 'cannot price' view AND folds into the total); a row in a LOCKED period gets an adjust()
    DELTA instead (the $ folds in; the immutable original keeps its marker, so a closed period is never edited).
    A row whose price STILL can't resolve is LEFT unpriced — honest, never a guessed number.

    DRY-RUN by default: returns a plan [{id, provider, model, in_tok, out_tok, cost, method}] (method ∈
    update|adjust|skip). `apply=True` commits through update()/adjust() — both audited + revision-bumped, never raw
    SQL. Filter to one `model` (e.g. 'glm-5.2') or sweep every unpriced model."""
    from ... import pricing
    from ...ledger import _KIND_TO_USD, dec
    from ...ledger_writer import current_ledger_writer_connection, run_ledger_write
    if current_ledger_writer_connection() is None:
        return run_ledger_write(lambda: reprice_unpriced(model, apply, actor))
    led = _admission_accessors.resolve__ledger()
    sql = "SELECT id FROM spend_events WHERE cost_basis=?"
    args = [BASIS_UNPRICED]
    if model:
        sql += " AND model=?"
        args.append(model)
    with _admission_accessors.resolve__lock():
        ids = [r[0] for r in led._conn.execute(sql, args).fetchall()]
    plan = []
    for eid in ids:
        row = led.get(eid) or {}
        prov, mdl = row.get("provider") or "?", row.get("model") or "?"
        kind = (row.get("cost_type") or "realtime").lower()
        col = _KIND_TO_USD.get(kind)
        in_t = None if row.get("in_tok") is None else int(row["in_tok"])
        out_t = None if row.get("out_tok") is None else int(row["out_tok"])
        ent = {"id": eid, "provider": prov, "model": mdl, "in_tok": in_t, "out_tok": out_t, "cost": None}
        if in_t is None or out_t is None:
            ent.update(method="skip", reason="unknown token usage; value unknown")
            plan.append(ent)
            continue
        if not col:
            ent.update(method="skip", reason=f"no money column for kind {kind!r}")
            plan.append(ent)
            continue
        try:                                              # price ONLY what resolves for this exact (provider, model)
            cost = float(pricing.realtime_cost(mdl, in_t, out_t, provider=prov) or 0.0)
        except Exception as e:                            # still ambiguous / genuinely unpriced → leave it, never guess
            ent.update(method="skip", reason=f"still unpriceable: {str(e)[:70]}")
            plan.append(ent)
            continue
        if cost <= 0:
            ent.update(method="skip", reason="resolved to $0")
            plan.append(ent)
            continue
        pr = pricing.price(mdl, provider=prov) or {}
        ent["cost"] = round(cost, 6)
        ent["_changes"] = {col: dec(cost), "cost_basis": BASIS_ESTIMATE,
                           "rate_in": pr.get("in_"), "rate_out": pr.get("out")}
        ent["method"] = "adjust" if led._is_period_locked(row.get("period")) else "update"
        plan.append(ent)
    if apply:
        for p in plan:
            if p.get("method") == "update":
                led.update(p["id"], p["_changes"], actor=actor, reason="reprice: model now priceable")
            elif p.get("method") == "adjust":
                led.adjust(p["id"], p["_changes"], actor=actor, reason="reprice: model now priceable")
    for p in plan:
        p.pop("_changes", None)                           # internal build detail — not part of the returned plan
    return plan

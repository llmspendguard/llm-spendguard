"""One-time migration: the legacy `charges` ledger (budget.py — float $, flat rows) → the financial-grade
`spend_events` (SpendLedger — integer micros, lifecycle, audit, unified attribution).

Faithful + IDEMPOTENT: each charge maps to one spend_event keyed by `charge:<rowid>` (re-running re-books nothing,
SpendLedger.record dedups on id). Money is preserved to the micro (Σ charges == Σ spend_events, asserted by the
caller). Attribution comes from the charge's gate-recorded `project` mapped to org+team via the taxonomy
(`conv._prior_org_team`), falling back to the unified `conv.resolve(conv_id)` when the project is blank — never a
regex guess. This does NOT touch the `charges` table (additive); it backfills the new ledger so consumers can move
onto it. Kept separate from SpendLedger so the ledger never imports the legacy store.
"""
import sqlite3
from . import config, conv, budget
from . import ledger as _ledger

# charge.kind → (record kind, is_meta). meta = spendguard's OWN realtime LLM use → realtime micros, flagged is_meta.
_KIND = {"realtime": ("realtime", 0), "batch": ("batch", 0), "meta": ("realtime", 1),
         "remote": ("remote", 0), "est_chat": ("est_chat", 0)}


def _is_marker(model):
    """A reconciliation row inserted by budget.record_reconciled carries a parenthesized MARKER model
    (e.g. '(provider-batch)') instead of a real model — it's provider-truth, not a metered call."""
    return bool(model) and model.startswith("(")


def to_spend_events(led=None, src_path=None, since=None):
    """Migrate every `charges` row into `spend_events`. Returns stats incl. both totals for the caller's Σ check.
    `led` — a SpendLedger (defaults to one on the same db); `src_path` — charges db (defaults to config.db_path())."""
    led = led or _ledger.SpendLedger()
    src = sqlite3.connect(src_path or config.db_path())
    src.row_factory = sqlite3.Row
    where, args = "", []
    if since:
        where, args = " WHERE day >= ?", [since]
    rows = src.execute("SELECT rowid AS rid, ts, day, provider, model, kind, cost, project, conv_id "
                       "FROM charges" + where, args).fetchall()
    _seg = {}                                                  # segments/store read LAZILY, once, only if a charge lacks a project

    def _resolve(conv_id):
        if "segs" not in _seg:                                 # first miss → read transcripts once (cached for the run)
            _seg["segs"], _seg["store"] = conv.segments(), conv._seg_get_all()
        return conv.resolve({"conv_id": conv_id}, segs=_seg["segs"], store=_seg["store"])
    skipped = 0
    src_usd = 0.0
    events = []
    # PASS 1 — build events + resolve attribution. Done BEFORE the bulk write txn so transcript/learn reads never
    # contend with the open ledger transaction (that contention is a self-deadlock on the same sqlite file).
    for r in rows:
        cost = float(r["cost"] or 0)
        if not cost:
            skipped += 1
            continue
        src_usd += cost
        ckind = (r["kind"] or "realtime").lower()
        rec_kind, is_meta = _KIND.get(ckind, ("realtime", 0))
        proj = (r["project"] or "").strip().lower()
        org, team = conv._prior_org_team(proj) if proj else ("", "")
        how, asource = "charge-project", "gate"
        if not org and r["conv_id"]:                           # no project tag → unified resolver (agentic, recorded)
            sc = _resolve(r["conv_id"])
            org = sc.get("org") or org
            team = team or sc.get("team") or ""
            proj = proj or sc.get("project") or ""
            how, asource = sc.get("how") or "resolve", sc.get("source") or "resolve"
        reconciled = 1 if _is_marker(r["model"]) else 0
        events.append({
            "kind": rec_kind, "usd": cost,
            "provider": r["provider"], "model": r["model"],
            "occurred_at": r["ts"], "ts_utc": r["ts"],
            "conv_id": r["conv_id"] or "",
            "org": org, "team": team, "project_primary": proj, "projects": [proj] if proj else [],
            "is_meta": is_meta, "reconciled": reconciled,
            "recon_marker": r["model"] if reconciled else None,
            "status": "reconciled" if reconciled else "posted",
            "cost_basis": "reconciled" if reconciled else ("meta" if is_meta else "gate"),
            "billed": 1,
            "source": "migrate:charges", "recorded_by": "migrate:charges",
            "dedup_key": "charge:%d" % r["rid"],               # stable + unique per source row → idempotent re-run
            "attr_how": how, "attr_source": asource,
        })
    # PASS 2 — bulk insert (one txn; every row still individually audited).
    n = 0
    with led.bulk():
        for ev in events:
            led.record(ev)
            n += 1
    dst_usd = led.sum_usd(source="migrate:charges")
    return {"charges_rows": len(rows), "migrated": n, "skipped_zero": skipped,
            "src_total_usd": round(src_usd, 2), "dst_total_usd": round(dst_usd, 2),
            "delta_usd": round(src_usd - dst_usd, 6)}

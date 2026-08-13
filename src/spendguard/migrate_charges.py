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
from . import config, conv, budget, ledger_sync
from . import ledger as _ledger

# charge.kind → (record kind, is_meta). meta = spendguard's OWN realtime LLM use → realtime micros, flagged is_meta.
_KIND = {"realtime": ("realtime", 0), "batch": ("batch", 0), "meta": ("realtime", 1),
         "remote": ("remote", 0), "est_chat": ("est_chat", 0)}

# Reconciliation rows carry a KNOWN sentinel model (not a real model). Sourced from the constants that WRITE them, so
# this stays in lock-step if a new marker is added — single source of truth, no "(...)" heuristic to misfire.
_RECON_MARKERS = frozenset({budget._RECONCILED, ledger_sync._RT_MARKER})


def _is_marker(model):
    """A reconciliation row (budget.record_reconciled / realtime backfill) carries a known sentinel model instead of a
    real model — it's provider-truth, not a metered call. DETERMINISTIC exact match against the known sentinels
    (NOT a 'starts with (' guess, which could misfire on a future real model name)."""
    return model in _RECON_MARKERS


def to_spend_events(led=None, src_path=None, since=None):
    """Migrate every `charges` row into `spend_events`. Returns stats incl. both totals for the caller's Σ check.
    `led` — a SpendLedger (defaults to one on the same db); `src_path` — charges db (defaults to config.db_path())."""
    led = led or _ledger.SpendLedger()
    src = sqlite3.connect(src_path or config.db_path())
    src.row_factory = sqlite3.Row
    where, args = "", []
    if since:
        where, args = " WHERE day >= ?", [since]
    # basis/intent/actor are optional (older ledgers predate them) — SELECT only what THIS db has and read the
    # rest as blank, so the migration never assumes a column shape it cannot see (PRAGMA is the ground truth).
    have = {c[1] for c in src.execute("PRAGMA table_info(charges)")}
    opt = [c for c in ("basis", "intent", "actor") if c in have]
    sel = ("rowid AS rid, ts, day, provider, model, kind, cost, project, conv_id, key_fp"
           + "".join(", " + c for c in opt))
    rows = src.execute(f"SELECT {sel} FROM charges" + where, args).fetchall()
    _seg = {}                                                  # segments/store read LAZILY, once, only if a charge lacks a project

    def _resolve(conv_id):
        if "segs" not in _seg:                                 # first miss → read transcripts once (cached for the run)
            _seg["segs"], _seg["store"] = conv.segments(), conv._seg_get_all()
        return conv.resolve({"conv_id": conv_id}, segs=_seg["segs"], store=_seg["store"])
    skipped = 0
    src_usd = 0.0
    events = []
    QUAR, UNPR = budget.QUARANTINE_CONV, budget.UNPRICED_CONV

    def _opt(row, key):
        """An optional charge column → its value, or '' when this db predates the column."""
        return (row[key] if key in row.keys() else "") or ""

    # PASS 1 — build events + resolve attribution. Done BEFORE the bulk write txn so transcript/learn reads never
    # contend with the open ledger transaction (that contention is a self-deadlock on the same sqlite file).
    for r in rows:
        cost = float(r["cost"] or 0)
        conv_id = r["conv_id"] or ""
        charge_basis = _opt(r, "basis").strip().lower()
        # UNPRICED — the call HAPPENED but its price is unknown (cost 0, marked). Migrated as a $0 forensic row,
        # NOT skipped: "we couldn't price it" is a different claim from "it was free". Requires cost 0, so a
        # (hypothetical) priced row wearing this marker keeps its money instead of being zeroed.
        is_unpriced = (not cost) and (conv_id == UNPR or charge_basis == budget.BASIS_UNPRICED)
        if not cost and not is_unpriced:
            skipped += 1                                       # a genuine $0, unmarked row carries no money and no claim
            continue
        ckind = (r["kind"] or "realtime").lower()
        rec_kind, is_meta = _KIND.get(ckind, ("realtime", 0))
        reconciled = 1 if _is_marker(r["model"]) else 0
        is_quarantine = (conv_id == QUAR)                      # impossible estimate → voided from totals, kept for forensics
        proj = (r["project"] or "").strip().lower()
        org, team = conv._prior_org_team(proj) if proj else ("", "")
        how, asource = "charge-project", "gate"
        if not org and conv_id:                                # no project tag → unified resolver (agentic, recorded)
            sc = _resolve(conv_id)
            org = sc.get("org") or org
            team = team or sc.get("team") or ""
            proj = proj or sc.get("project") or ""
            how, asource = sc.get("how") or "resolve", sc.get("source") or "resolve"
        # cost_basis carries the WHAT-KIND-OF-NUMBER axis (estimate/billed/assumed/reconstructed/unpriced) — the
        # charge's own declared basis. The ROLE (meta/reconciled) is NOT a basis; it lives in the is_meta /
        # reconciled flags + status. Nothing reads the old 'gate/meta/reconciled' basis values (verified), and
        # blank stays blank ('unlabelled'), never silently promoted to 'billed'.
        if is_unpriced:
            cost_basis = budget.BASIS_UNPRICED
        elif charge_basis in budget.BASES:
            cost_basis = charge_basis
        else:
            cost_basis = ""
        status = "void" if is_quarantine else ("reconciled" if reconciled else "posted")
        ev = {
            "provider": r["provider"], "model": r["model"],
            "occurred_at": r["ts"], "ts_utc": r["ts"],
            "conv_id": conv_id,
            "org": org, "team": team, "project_primary": proj, "projects": [proj] if proj else [],
            "key_fp": r["key_fp"] or "",                       # which API key served it (per-key spend)
            "intent": _opt(r, "intent"), "actor": _opt(r, "actor"),   # the forensic pair: what it bought · what ran it
            "is_meta": is_meta, "reconciled": reconciled,
            "recon_marker": r["model"] if reconciled else None,
            "status": status, "cost_basis": cost_basis,
            "billed": 1,
            "source": "migrate:charges", "recorded_by": "migrate:charges",
            "dedup_key": "charge:%d" % r["rid"],               # stable + unique per source row → idempotent re-run
            "attr_how": how, "attr_source": asource,
        }
        if is_unpriced:
            ev["usd"] = None                                   # price unknown → NO money column; a $0 forensic marker
        else:
            ev["kind"] = rec_kind
            ev["usd"] = cost                                   # real cost (incl. negative true-down corrections)
            src_usd += cost
        events.append(ev)
    # PASS 2 — bulk insert (one txn; every row still individually audited).
    n = 0
    with led.bulk():
        for ev in events:
            led.record(ev)
            n += 1
    # include_void=True: quarantined-impossible rows land as status=void, so the conservation total must count
    # them too — else delta shows a phantom loss exactly equal to the quarantined $ (here ~$10.5k of it).
    dst_usd = led.sum_usd(source="migrate:charges", include_void=True)
    return {"charges_rows": len(rows), "migrated": n, "skipped_zero": skipped,
            "src_total_usd": round(src_usd, 2), "dst_total_usd": round(dst_usd, 2),
            "delta_usd": round(src_usd - dst_usd, 6)}


def run_cutover(db_path=None):
    """THE clear, runnable migration: rebuild `spend_events` from `charges` under the v6 exact-Decimal
    schema, and PROVE the sum is preserved. One command, start to finish — this is what was missing (the
    migration existed only as a buried backfill function with no runner and no cutover).

    LOSSLESS + CLEAN. Before touching anything it snapshots the WHOLE db file (the real recovery path — the
    existing, tested snapshot machinery), then RENAMES any existing `spend_events` aside to
    `spend_events_precutover`. Both matter, and both were proven necessary on the live db: the existing table
    was STALE/PARTIAL (24,642 charges rows at the old schema while `charges` now holds 45,941), so a rebuild
    ONTO it would dedup-append and leave old rows with the old mapping; and it held a few NON-charges rows (2,
    from guard/adjust ops) that a charges-only rebuild drops — the file snapshot preserves every one. The
    rebuild then reads `charges` (the complete float source of record) and records each row as exact Decimal.

    Returns stats incl. the EXACT Decimal residual (Σ charges − Σ spend_events), which must be $0.00 to trust
    the cutover. Σ is over EVERY dollar (include_void), so quarantined-impossible rows are proven carried too."""
    from decimal import Decimal
    from . import ledger as _ledger
    from . import budget as _budget
    path = db_path or config.db_path()

    # Whole-file snapshot FIRST — the lossless recovery path (holds every prior row, incl. non-charges ones).
    snap = _budget.snapshot_once("cutover") if not db_path else _budget.snapshot(reason="cutover")
    con = sqlite3.connect(path)
    if [r[1] for r in con.execute("PRAGMA table_info(spend_events)")]:
        # Move any existing table aside so the rebuild is CLEAN — it never dedup-appends onto stale rows. One
        # fixed name (no provenance guess); the file snapshot above is what guarantees nothing is lost.
        con.execute("DROP TABLE IF EXISTS spend_events_precutover")    # a prior aborted cutover's backup
        con.execute("ALTER TABLE spend_events RENAME TO spend_events_precutover")
        con.commit()
    con.close()

    led = _ledger.SpendLedger(db_path=path)                            # constructs the fresh v6 schema
    stats = to_spend_events(led=led, src_path=path)

    # PROVE Σ EXACTLY, both sides in Decimal (charges is float text → Decimal per row, summed exactly).
    con = sqlite3.connect(path)
    src_dec = sum((Decimal(str(r[0])) for r in con.execute("SELECT cost FROM charges WHERE cost!=0")), Decimal(0))
    con.close()
    # include_void=True: the proof is "did EVERY charge dollar arrive", and quarantined-impossible rows arrive
    # as status=void. src_dec counts them (cost!=0), so dst must too, or the residual would be a phantom.
    dst_dec = Decimal(led.sum_dec(include_void=True))   # EXACT (not the float sum_usd) — the reconciliation primitive
    stats.update(db_snapshot=snap, backup_table="spend_events_precutover", src_exact=str(src_dec),
                 dst_exact=str(dst_dec), residual=str(src_dec - dst_dec), reconciles=(src_dec == dst_dec))
    return stats

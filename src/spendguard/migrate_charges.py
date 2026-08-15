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

# The charge→event field mapping (kind/role/basis, incl. which models are reconciliation markers) lives in ONE
# place — budget.charge_to_event — shared with the live gate write so the two can never drift. An earlier copy
# here used an INCOMPLETE marker set (2 of 4), so two realtime-reconcile marker kinds were mis-flagged as
# workload; using budget's canonical _MARKER_MODELS via charge_to_event fixes that. This module adds only
# attribution + identity + provenance on top of the shared mapping.


def to_spend_events(led=None, src_path=None, since=None):
    """Migrate every `charges` row into `spend_events`. Returns stats incl. both totals for the caller's Σ check.
    `led` — a SpendLedger (defaults to one on the same db); `src_path` — charges db (defaults to config.db_path())."""
    led = led or _ledger.SpendLedger()
    # `src` is only read in this prologue (schema probe + the one SELECT); the rest of the migration works off
    # the fetched `rows`. Close it deterministically — including on the raise below — so the migration doesn't
    # leak the charges-db connection (sqlite3's context manager commits but does NOT close, hence try/finally).
    src = sqlite3.connect(src_path or config.db_path())
    try:
        src.row_factory = sqlite3.Row
        if not src.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='charges'").fetchone():
            raise ValueError("no `charges` table — the cutover is complete and it was dropped; spend_events is the "
                             "sole ledger. There is nothing to migrate. (run_cutover() detects this and no-ops; call "
                             "it, not this, from the CLI.)")
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
    finally:
        src.close()
    _seg = {}                                                  # segments/store read LAZILY, once, only if a charge lacks a project

    def _resolve(conv_id):
        if "segs" not in _seg:                                 # first miss → read transcripts once (cached for the run)
            _seg["segs"], _seg["store"] = conv.segments(), conv._seg_get_all()
        return conv.resolve({"conv_id": conv_id}, segs=_seg["segs"], store=_seg["store"])
    skipped = 0
    src_usd = 0.0
    events = []
    UNPR = budget.UNPRICED_CONV

    def _opt(row, key):
        """An optional charge column → its value, or '' when this db predates the column."""
        return (row[key] if key in row.keys() else "") or ""

    # PASS 1 — build events + resolve attribution. Done BEFORE the bulk write txn so transcript/learn reads never
    # contend with the open ledger transaction (that contention is a self-deadlock on the same sqlite file).
    for r in rows:
        cost = float(r["cost"] or 0)
        conv_id = r["conv_id"] or ""
        # A genuine $0 that is NOT an unpriced marker carries no money and no claim → skip. Unpriced ($0, marked)
        # is KEPT: "we couldn't price it" is a different claim from "it was free" (charge_to_event maps it).
        is_unpriced_zero = (not cost) and (conv_id == UNPR or _opt(r, "basis").strip().lower() == budget.BASIS_UNPRICED)
        if not cost and not is_unpriced_zero:
            skipped += 1
            continue
        # THE money/role/basis mapping — the SAME function the live gate write uses, so they cannot drift.
        ev = budget.charge_to_event(r["provider"], r["model"], r["kind"], cost, conv_id=conv_id,
                                    basis=_opt(r, "basis"), intent=_opt(r, "intent"), actor=_opt(r, "actor"),
                                    key_fp=r["key_fp"] or "")
        # attribution (agentic, recorded): charge's project → org/team via the prior map, else the unified resolver
        proj = (r["project"] or "").strip().lower()
        org, team = conv._prior_org_team(proj) if proj else ("", "")
        how, asource = "charge-project", "gate"
        if (not org or not team) and conv_id:                  # missing org OR team → unified resolver (agentic).
            sc = _resolve(conv_id)                             # a prior-map org WITH an empty team used to skip
            org = sc.get("org") or org                        # this entirely, leaving team blank when it was
            team = team or sc.get("team") or ""               # resolvable. The body fills only the empty fields.
            proj = proj or sc.get("project") or ""
            how, asource = sc.get("how") or "resolve", sc.get("source") or "resolve"
        ev.update({
            "occurred_at": r["ts"], "ts_utc": r["ts"],
            "org": org, "team": team, "project_primary": proj, "projects": [proj] if proj else [],
            "source": "migrate:charges", "recorded_by": "migrate:charges",
            "dedup_key": "charge:%d" % r["rid"],               # stable + unique per source row → idempotent re-run
            "attr_how": how, "attr_source": asource,
        })
        if ev.get("usd") is not None:                          # unpriced rows carry no money → not summed
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

    # ALREADY CUT OVER — refuse, touching NOTHING. Once `charges` has been dropped, spend_events IS the ledger,
    # and re-running would be catastrophic: the rename step below moves the LIVE spend_events aside (and DROPs the
    # real pre-cutover backup first), then to_spend_events crashes on the missing `charges`, leaving an EMPTY
    # ledger. This is the "old path made impossible" — the migration is a no-op after its source is gone.
    _probe = sqlite3.connect(path)
    _has_charges = bool(_probe.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='charges'").fetchone())
    _probe.close()
    if not _has_charges:
        return {"charges_rows": 0, "migrated": 0, "skipped_zero": 0, "already_cutover": True,
                "reconciles": True, "note": "charges retired — spend_events is the sole ledger; nothing to migrate"}

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

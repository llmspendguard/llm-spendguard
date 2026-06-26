"""SpendLedger — the SINGLE gateway to spend data (an in-process context/data provider; MCP-style, not a server).

Every read/write of spend goes through this class. No consumer writes raw SQL — the class owns the schema and ALL
queries/joins, returns typed dicts, and routes the agentic ATTRIBUTION through one path. Deterministic SQL for
queries; the LLM is used ONLY for attribution (meaning), recorded so re-runs read it (repeatable).

Financial-systems design (Xero / QuickBooks-style — flexibility with controls):
- **Money is integer micro-USD** (`*_micros`, ×1e6) — never float; sums are exact.
- **Time** is UTC-canonical (`ts_utc`) + source-local (`tz`/`local_datetime`); accounting `day`/`period` are derived in
  the reporting tz (`SPENDGUARD_REPORTING_TZ`); transaction date (`occurred_at`) ≠ posting date (`recorded_at`).
- **Multi-pass enrichment with controls** — a spend event is MUTABLE across passes (ingest → attribute → reconcile)
  until its period is LOCKED (per-period `lock_date` / `status=locked`); then it's immutable and corrections are
  reverse/adjust entries. Lifecycle `status`: draft → posted → reconciled → locked.
- **Integrity lives in `spend_audit`** — every change appends to that append-only, **hash-chained** log
  (who/when/field/old→new/pass); `verify_audit_chain()` proves it wasn't altered. The live row carries no hash.
- **Self-contained record + link-ids** — snapshots cost/attribution/rates + `seg_id`/`call_id`/`conv_id`/`batch_id`/`model`.
"""
import os
import json
import sqlite3
import hashlib
import datetime
from . import config

SCHEMA_VERSION = 4


class LockedError(Exception):
    """Raised when a write would modify a locked row or post into a locked period (use reverse/adjust)."""


_DDL_EVENTS = """
CREATE TABLE IF NOT EXISTS spend_events (
  -- identity / dedup
  id            TEXT PRIMARY KEY,
  dedup_key     TEXT,
  source        TEXT,
  content_hash  TEXT,
  schema_version INTEGER DEFAULT 4,
  -- time
  ts_utc        TEXT,
  occurred_at   TEXT,                      -- transaction date (UTC)
  recorded_at   TEXT,                      -- posting date (UTC)
  tz            TEXT,
  local_datetime TEXT,
  day           TEXT,                      -- accounting day (reporting tz)
  period        TEXT,                      -- accounting period (reporting tz)
  eligibility_window TEXT,
  window_start  TEXT,
  window_end    TEXT,
  -- money: integer micro-units of `currency`
  currency      TEXT DEFAULT 'USD',
  batch_micros          INTEGER DEFAULT 0,
  realtime_micros       INTEGER DEFAULT 0,
  est_chat_micros       INTEGER DEFAULT 0,
  remote_compute_micros INTEGER DEFAULT 0,
  subscription_micros   INTEGER DEFAULT 0,
  cost_type     TEXT,
  billed        INTEGER DEFAULT 1,
  is_meta       INTEGER DEFAULT 0,
  cost_basis    TEXT,
  amount_confidence REAL,
  rate_in       REAL,
  rate_out      REAL,
  fx_rate       REAL,
  base_micros   INTEGER,
  -- provider / model
  provider      TEXT,
  model         TEXT,
  model_kind    TEXT,
  finish        TEXT,
  -- metering
  in_tok        INTEGER DEFAULT 0,
  out_tok       INTEGER DEFAULT 0,
  cache_read_tok  INTEGER DEFAULT 0,
  cache_write_tok INTEGER DEFAULT 0,
  reasoning_tok INTEGER DEFAULT 0,
  num_calls     INTEGER DEFAULT 1,
  num_items     INTEGER DEFAULT 0,
  latency       REAL,
  -- attribution result
  org           TEXT,
  team          TEXT,
  projects      TEXT,
  project_primary TEXT,
  member_ref    TEXT,
  -- billing / multi-entity
  account_id    TEXT,
  customer_id   TEXT,
  cost_center   TEXT,
  engagement    TEXT,
  billable      INTEGER DEFAULT 0,
  invoice_id    TEXT,
  -- lineage / evidence / links
  conv_id       TEXT,
  seg_id        TEXT,
  call_id       TEXT,
  cwd           TEXT,
  batch_id      TEXT,
  from_message_ids  TEXT,
  prior_message_ids TEXT,
  post_message_ids  TEXT,
  script        TEXT,
  repo          TEXT,
  host          TEXT,
  prompt_hash   TEXT,
  prompt_snip   TEXT,
  output_snip   TEXT,
  evidence_uri  TEXT,
  -- attribution audit (snapshot of the determination)
  attr_what     TEXT,
  attr_why      TEXT,
  attr_how      TEXT,
  attr_reason   TEXT,
  attr_confidence REAL,
  attr_source   TEXT,
  attr_model    TEXT,
  attr_ts       TEXT,
  attr_version  TEXT,
  -- record provenance
  recorded_by   TEXT,
  ingest_version TEXT,
  -- lifecycle (mutable until locked; correct-by-reversal after lock)
  status        TEXT DEFAULT 'draft',      -- draft | posted | reconciled | locked | reversed | void
  revision      INTEGER DEFAULT 1,
  locked        INTEGER DEFAULT 0,
  locked_at     TEXT,
  lock_reason   TEXT,
  reverses_id   TEXT,                      -- this entry reverses that one
  adjusts_id    TEXT,                      -- this entry adjusts that one
  superseded_by TEXT,
  -- reconciliation / close
  reconciled    INTEGER DEFAULT 0,
  reconciled_vs TEXT,
  reconciled_at TEXT,
  reconciliation_id TEXT,
  gap_flag      TEXT,
  period_closed INTEGER DEFAULT 0,
  recon_marker  TEXT,
  -- quality / governance
  quality       TEXT,
  quality_src   TEXT,
  quality_conf  REAL,
  cache_hit     INTEGER DEFAULT 0,
  savings_cv    REAL,
  -- free
  tags          TEXT
)
"""

_DDL_AUDIT = """
CREATE TABLE IF NOT EXISTS spend_audit (
  id        INTEGER PRIMARY KEY AUTOINCREMENT,
  event_id  TEXT,
  ts        TEXT,
  actor     TEXT,
  pass      TEXT,                          -- ingest | attribute | reconcile | update | lock | reverse | adjust
  field     TEXT,
  old_value TEXT,
  new_value TEXT,
  reason    TEXT,
  prev_hash TEXT,
  row_hash  TEXT                           -- = sha256(content + prev_hash); the append-only chain
)
"""

_DDL_LOCKS = """
CREATE TABLE IF NOT EXISTS ledger_locks (
  period    TEXT PRIMARY KEY,              -- YYYY-MM closed; everything <= MAX(period) is locked (the lock date)
  locked_at TEXT,
  reason    TEXT,
  actor     TEXT
)
"""

MICRO_COLS = ("batch_micros", "realtime_micros", "est_chat_micros", "remote_compute_micros", "subscription_micros")
BILLED_MICRO_COLS = ("batch_micros", "realtime_micros", "remote_compute_micros", "subscription_micros")
_KIND_TO_MICRO = {"batch": "batch_micros", "realtime": "realtime_micros",
                  "est_chat": "est_chat_micros", "est-chat": "est_chat_micros", "estchat": "est_chat_micros",
                  "remote": "remote_compute_micros", "remote_compute": "remote_compute_micros", "gpu": "remote_compute_micros",
                  "subscription": "subscription_micros", "sub": "subscription_micros"}
_MICRO_TO_KIND = {"batch_micros": "batch", "realtime_micros": "realtime", "est_chat_micros": "est_chat",
                  "remote_compute_micros": "remote_compute", "subscription_micros": "subscription"}
_JSON_COLS = ("projects", "from_message_ids", "prior_message_ids", "post_message_ids", "tags")
_EVIDENCE = ("source", "conv_id", "batch_id", "script", "model", "prompt_hash", "in_tok", "out_tok", "attr_what")
_AUDIT_FIELDS = ("event_id", "ts", "actor", "pass", "field", "old_value", "new_value", "reason")
_INDEXES = ("org", "day", "period", "conv_id", "source", "batch_id", "dedup_key", "reconciled", "model_kind", "status")
_PROTECTED = {"id", "ts_utc", "occurred_at", "day", "period", "currency", "source", "dedup_key"}   # not changed by update()


def _now_utc():
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")


def _reporting_tz():
    return os.getenv("SPENDGUARD_REPORTING_TZ") or "UTC"


def _day_period(ts_iso, tzname):
    try:
        dt = datetime.datetime.fromisoformat(ts_iso)
        if tzname and tzname != "UTC":
            from zoneinfo import ZoneInfo
            dt = dt.astimezone(ZoneInfo(tzname))
        return dt.date().isoformat(), dt.strftime("%Y-%m")
    except Exception:
        return (ts_iso or "")[:10], (ts_iso or "")[:7]


def micros(usd):
    return int(round(float(usd) * 1_000_000))


def to_usd(micros_val):
    return round((micros_val or 0) / 1_000_000, 6)


class SpendLedger:
    """The one door to spend_events: SCRUD + queries + lifecycle/audit. Mutable until locked; integrity in spend_audit."""

    def __init__(self, db_path=None):
        self.db_path = db_path or config.db_path()
        self._conn = sqlite3.connect(self.db_path, timeout=10, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._cols = self._ensure_schema()

    def _ensure_schema(self):
        for ddl in (_DDL_EVENTS, _DDL_AUDIT, _DDL_LOCKS):
            self._conn.execute(ddl)
        for ix in _INDEXES:
            self._conn.execute(f"CREATE INDEX IF NOT EXISTS idx_se_{ix} ON spend_events({ix})")
        self._conn.execute("CREATE INDEX IF NOT EXISTS idx_audit_event ON spend_audit(event_id)")
        self._conn.commit()
        return [r[1] for r in self._conn.execute("PRAGMA table_info(spend_events)")]

    @staticmethod
    def _evidence_id(ev):
        key = ev.get("dedup_key") or "|".join(str(ev.get(k) or "") for k in _EVIDENCE)
        return hashlib.sha256(key.encode()).hexdigest()[:20]

    # ── lock control ──
    def _lock_date(self):
        r = self._conn.execute("SELECT MAX(period) FROM ledger_locks").fetchone()
        return r[0] if r else None

    def _is_period_locked(self, period):
        ld = self._lock_date()
        return bool(period and ld and period <= ld)

    def _is_locked(self, row):
        return bool(row["status"] == "locked" or row["locked"] or self._is_period_locked(row["period"]))

    # ── audit (append-only, hash-chained) ──
    def _audit(self, event_id, actor, pass_, field, old, new, reason):
        rec = {"event_id": event_id, "ts": _now_utc(), "actor": actor or "?", "pass": pass_,
               "field": field, "old_value": None if old is None else str(old),
               "new_value": None if new is None else str(new), "reason": reason or ""}
        prev = self._conn.execute("SELECT row_hash FROM spend_audit ORDER BY id DESC LIMIT 1").fetchone()
        prev_hash = prev[0] if prev else ""
        body = json.dumps({k: rec[k] for k in _AUDIT_FIELDS}, sort_keys=True, default=str)
        rec["prev_hash"] = prev_hash
        rec["row_hash"] = hashlib.sha256((body + prev_hash).encode()).hexdigest()
        cols = list(rec.keys())
        self._conn.execute(f"INSERT INTO spend_audit ({','.join(cols)}) VALUES ({','.join('?' for _ in cols)})",
                           [rec[c] for c in cols])

    # ── C: create (a draft event; logged to the audit chain) ──
    def record(self, ev):
        ev = dict(ev)
        kind = (ev.pop("kind", None) or "").lower()
        usd = ev.pop("usd", None)
        if kind and usd is not None:
            col = _KIND_TO_MICRO.get(kind)
            if not col:
                raise ValueError(f"unknown spend kind {kind!r}; expected batch | realtime | est_chat | remote | subscription")
            ev[col] = micros(usd)
        for mc in MICRO_COLS:
            ucol = mc.replace("_micros", "_usd")
            if ucol in ev and ev.get(mc) is None:
                ev[mc] = micros(ev.pop(ucol))
        nz = [c for c in MICRO_COLS if int(ev.get(c) or 0)]
        if not nz:
            raise ValueError("spend event has no cost in any micros column")
        if not ev.get("dedup_key") and not ev.get("source"):
            raise ValueError("spend event needs a dedup_key or a source")
        ev.setdefault("currency", "USD")
        ev.setdefault("cost_type", _MICRO_TO_KIND[nz[0]] if len(nz) == 1 else None)
        now = _now_utc()
        ev.setdefault("ts_utc", now)
        ev.setdefault("recorded_at", now)
        ev.setdefault("occurred_at", ev["ts_utc"])
        loc = datetime.datetime.now().astimezone()
        ev.setdefault("tz", getattr(loc.tzinfo, "key", None) or loc.tzname() or "")
        ev.setdefault("local_datetime", loc.isoformat(timespec="seconds"))
        d, p = _day_period(ev["occurred_at"], _reporting_tz())
        ev.setdefault("day", d)
        ev.setdefault("period", p)
        if ev.get("model") and (ev.get("rate_in") is None or ev.get("rate_out") is None):
            try:
                from . import pricing
                pr = pricing.price(ev["model"]) or {}
                bt = ev.get("cost_type") == "batch"
                ev.setdefault("rate_in", pr.get("batch_in" if bt else "in_"))
                ev.setdefault("rate_out", pr.get("batch_out" if bt else "out"))
            except Exception:
                pass
        ev.setdefault("schema_version", SCHEMA_VERSION)
        ev.setdefault("status", "draft")
        ev.setdefault("revision", 1)
        ev["id"] = ev.get("id") or self._evidence_id(ev)
        if self._conn.execute("SELECT 1 FROM spend_events WHERE id=?", (ev["id"],)).fetchone():
            return ev["id"]                                   # already booked — no double-count
        if self._is_period_locked(ev.get("period")):
            raise LockedError(f"period {ev.get('period')} is locked — post an adjustment to the open period")
        for jc in _JSON_COLS:
            if jc in ev and not isinstance(ev.get(jc), (str, type(None))):
                ev[jc] = json.dumps(ev[jc])
        cols = [c for c in self._cols if c in ev]
        self._conn.execute(f"INSERT INTO spend_events ({','.join(cols)}) VALUES ({','.join('?' for _ in cols)})",
                           [ev.get(c) for c in cols])
        self._audit(ev["id"], ev.get("recorded_by") or ev.get("source") or "?", "ingest", "(create)", None,
                    f"{ev.get('cost_type')} {to_usd(sum(int(ev.get(c) or 0) for c in MICRO_COLS))} USD", "ingested")
        self._conn.commit()
        return ev["id"]

    # ── U: update an OPEN row (refuses if locked; logs every field) ──
    def update(self, eid, changes, actor="?", reason="", pass_="update"):
        row = self._conn.execute("SELECT * FROM spend_events WHERE id=?", (eid,)).fetchone()
        if not row:
            raise ValueError(f"no spend event {eid!r}")
        if self._is_locked(row):
            raise LockedError(f"event {eid} is locked (status={row['status']}, period={row['period']}) — use reverse/adjust")
        applied = 0
        for field, new in changes.items():
            if field in _PROTECTED:
                raise ValueError(f"{field!r} is immutable (identity/period) — reverse/adjust instead")
            if field not in self._cols:
                raise ValueError(f"unknown column {field!r}")
            old = row[field]
            nv = json.dumps(new) if field in _JSON_COLS and not isinstance(new, (str, type(None))) else new
            if nv == old:
                continue
            self._conn.execute(f"UPDATE spend_events SET {field}=? WHERE id=?", (nv, eid))
            self._audit(eid, actor, pass_, field, old, nv, reason)
            applied += 1
        if applied:
            self._conn.execute("UPDATE spend_events SET revision=revision+1 WHERE id=?", (eid,))
        self._conn.commit()
        return applied

    # ── the attribution PASS (draft → posted). Deterministic plumbing; the agentic determiner feeds it. ──
    def attribute(self, eid, *, org=None, team=None, projects=None, project_primary=None, member_ref=None,
                  seg_id=None, attr_what=None, attr_why=None, attr_how=None, attr_reason=None,
                  attr_confidence=None, attr_source=None, attr_model=None, actor="attribution", reason=""):
        """Apply an attribution determination to an OPEN event (org/team/projects + `attr_*`, status → posted), logged
        to `spend_audit`. The agentic determiner (cwd-anchored, seg_attribution join + LLM, convergence loop) computes
        the values and calls THIS — so every attribution is recorded identically + traceably, and a re-run reads the
        recorded determination rather than re-asking the LLM."""
        changes = {k: v for k, v in {
            "org": org, "team": team, "projects": projects, "project_primary": project_primary,
            "member_ref": member_ref, "seg_id": seg_id, "attr_what": attr_what, "attr_why": attr_why,
            "attr_how": attr_how, "attr_reason": attr_reason, "attr_confidence": attr_confidence,
            "attr_source": attr_source, "attr_model": attr_model, "attr_ts": _now_utc(), "status": "posted",
        }.items() if v is not None}
        return self.update(eid, changes, actor=actor, reason=reason, pass_="attribute")

    # ── period close (lock) ──
    def lock_period(self, period, reason="", actor="?"):
        """Close a period: everything in/before `period` becomes immutable. Returns the count locked."""
        self._conn.execute("INSERT OR REPLACE INTO ledger_locks (period,locked_at,reason,actor) VALUES (?,?,?,?)",
                           (period, _now_utc(), reason, actor))
        rows = self._conn.execute("SELECT id FROM spend_events WHERE period<=? AND status!='locked'", (period,)).fetchall()
        for (rid,) in rows:
            self._conn.execute("UPDATE spend_events SET status='locked', locked=1, locked_at=?, lock_reason=? WHERE id=?",
                               (_now_utc(), reason, rid))
            self._audit(rid, actor, "lock", "status", None, "locked", reason or f"period {period} closed")
        self._conn.commit()
        return len(rows)

    # ── corrections after lock: reverse / adjust (new rows; never touch the locked one) ──
    def _clone_for_correction(self, eid, kind_field, actor, reason, negate=False, overrides=None):
        row = self._conn.execute("SELECT * FROM spend_events WHERE id=?", (eid,)).fetchone()
        if not row:
            raise ValueError(f"no spend event {eid!r}")
        ev = {k: row[k] for k in row.keys()}
        for jc in _JSON_COLS:                                 # deserialise so record() re-serialises cleanly
            if ev.get(jc):
                try:
                    ev[jc] = json.loads(ev[jc])
                except Exception:
                    pass
        for k in ("id", "row_hash", "prev_hash", "status", "locked", "locked_at", "lock_reason",
                  "revision", "ts_utc", "occurred_at", "recorded_at", "day", "period", "dedup_key"):
            ev.pop(k, None)
        if negate:
            for c in MICRO_COLS:
                ev[c] = -int(row[c] or 0)
        ev[kind_field] = eid
        ev["source"] = (row["source"] or "") + (":" + kind_field.split("_")[0])   # distinct id from the original
        ev.update(overrides or {})
        new_id = self.record(ev)                              # posts into the CURRENT open period
        self._audit(new_id, actor, "reverse" if negate else "adjust", kind_field, None, eid, reason)
        self._conn.commit()
        return new_id

    def reverse(self, eid, actor="?", reason=""):
        """Post a reversing entry (negates the original) into the open period. The original stays untouched."""
        return self._clone_for_correction(eid, "reverses_id", actor, reason, negate=True)

    def adjust(self, eid, changes, actor="?", reason=""):
        """Post a corrected entry (the original + `changes`) into the open period, linked via adjusts_id."""
        return self._clone_for_correction(eid, "adjusts_id", actor, reason, negate=False, overrides=changes)

    # ── R: read ──
    def get(self, eid):
        r = self._conn.execute("SELECT * FROM spend_events WHERE id=?", (eid,)).fetchone()
        return self._row(r) if r else None

    def history(self, eid):
        """The full change timeline for an event (from spend_audit)."""
        return [{k: r[k] for k in r.keys()}
                for r in self._conn.execute("SELECT * FROM spend_audit WHERE event_id=? ORDER BY id", (eid,))]

    def _row(self, r):
        d = {k: r[k] for k in r.keys()}
        for jc in _JSON_COLS:
            if d.get(jc):
                try:
                    d[jc] = json.loads(d[jc])
                except Exception:
                    pass
        return d

    def _where(self, since, until, where):
        sql, args = "", []
        if since:
            sql += " AND day >= ?"; args.append(since)
        if until:
            sql += " AND day <= ?"; args.append(until)
        for k, v in (where or {}).items():
            if k not in self._cols:
                raise ValueError(f"unknown filter column {k!r}")
            sql += f" AND {k} = ?"; args.append(v)
        return sql, args

    # ── S: search / query ──
    def query(self, since=None, until=None, where=None, limit=None):
        w, args = self._where(since, until, where)
        sql = "SELECT * FROM spend_events WHERE 1=1" + w + " ORDER BY day"
        if limit:
            sql += f" LIMIT {int(limit)}"
        return [self._row(r) for r in self._conn.execute(sql, args).fetchall()]

    # ── rollup: cost breakdown, billed vs est-value split (exact micros + usd; voided/reversed excluded) ──
    def rollup(self, group_by=None, since=None, until=None, where=None, include_meta=False):
        cols = [group_by] if isinstance(group_by, str) else list(group_by or [])
        for g in cols:
            if g not in self._cols:
                raise ValueError(f"unknown group_by column {g!r}")
        w, args = self._where(since, until, where)
        if not include_meta:
            w += " AND COALESCE(is_meta,0)=0"
        w += " AND COALESCE(status,'') NOT IN ('void')"       # void excluded; a reversed pair nets to 0 via its negation
        sums = ", ".join(f"SUM({c})" for c in MICRO_COLS)
        sel = (", ".join(cols) + ", " if cols else "") + sums + ", COUNT(*)"
        sql = f"SELECT {sel} FROM spend_events WHERE 1=1" + w + (" GROUP BY " + ", ".join(cols) if cols else "")

        def pack(row):
            vals = {c: int(row[len(cols) + i] or 0) for i, c in enumerate(MICRO_COLS)}
            billed = sum(vals[c] for c in BILLED_MICRO_COLS)
            out = {**vals, "billed_micros": billed, "est_value_micros": vals["est_chat_micros"], "n": row[-1]}
            out["billed_usd"] = to_usd(billed)
            out["est_value_usd"] = to_usd(vals["est_chat_micros"])
            for c in MICRO_COLS:
                out[c.replace("_micros", "_usd")] = to_usd(vals[c])
            return out
        rows = self._conn.execute(sql, args).fetchall()
        empty = pack([0] * (len(cols) + len(MICRO_COLS)) + [0])
        if not cols:
            return pack(rows[0]) if rows and rows[0][-1] else empty
        return {(tuple(row[i] for i in range(len(cols))) if len(cols) > 1 else row[0]): pack(row) for row in rows}

    def by_repo(self, repo, since=None, until=None):
        return self.rollup(since=since, until=until, where={"repo": repo})

    # ── integrity: the AUDIT LOG is hash-chained (not the live row) ──
    def verify_audit_chain(self):
        """Recompute the spend_audit hash chain; return (ok, first_bad_id|None). Proves the change log wasn't altered."""
        prev = ""
        for r in self._conn.execute("SELECT * FROM spend_audit ORDER BY id"):
            body = json.dumps({k: r[k] for k in _AUDIT_FIELDS}, sort_keys=True, default=str)
            if hashlib.sha256((body + prev).encode()).hexdigest() != r["row_hash"]:
                return False, r["id"]
            prev = r["row_hash"]
        return True, None

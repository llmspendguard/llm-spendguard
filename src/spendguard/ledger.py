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
import contextlib
import itertools
from decimal import Decimal, InvalidOperation, getcontext
from . import config

getcontext().prec = 34   # ample for exact money arithmetic (28 is the default; money never needs more than this)

SCHEMA_VERSION = 6


class LockedError(Exception):
    """Raised when a write would modify a locked row or post into a locked period (use reverse/adjust)."""


_DDL_EVENTS = """
CREATE TABLE IF NOT EXISTS spend_events (
  -- identity / dedup
  id            TEXT PRIMARY KEY,
  dedup_key     TEXT,
  source        TEXT,
  content_hash  TEXT,
  schema_version INTEGER DEFAULT 6,
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
  -- money: EXACT DECIMAL USD, stored as TEXT (canonical decimal string), summed with the dec_sum aggregate.
  -- NOT integer micros (truncated sub-micro real costs to $0) and NOT binary float (0.1+0.2!=0.3 over
  -- millions of sums). One category column per spend class keeps the five categories apart structurally.
  currency      TEXT DEFAULT 'USD',
  batch_usd             TEXT,
  realtime_usd          TEXT,
  est_chat_usd          TEXT,
  remote_compute_usd    TEXT,
  subscription_usd      TEXT,
  cost_type     TEXT,
  billed        INTEGER DEFAULT 1,
  is_meta       INTEGER DEFAULT 0,
  cost_basis    TEXT,
  amount_confidence REAL,
  rate_in       REAL,
  rate_out      REAL,
  fx_rate       REAL,
  base_usd      TEXT,
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
  key_fp        TEXT,                      -- which API key served the call (per-key spend; carried from charges)
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
  -- forensic pair (carried from charges): WHAT the money bought · WHAT RAN IT
  intent        TEXT,                      -- 'review:config.py', 'spendguard:cache-test'
  actor         TEXT,                      -- 'repo_review_panel.py:fan_out:238' (entrypoint:function:line)
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

USD_COLS = ("batch_usd", "realtime_usd", "est_chat_usd", "remote_compute_usd", "subscription_usd")
BILLED_USD_COLS = ("batch_usd", "realtime_usd", "remote_compute_usd", "subscription_usd")
# THE FIVE CATEGORIES STAY APART — a hard rule (never sum est-value into real $). Each cap and report reads
# the columns for ITS category, never the whole row:
#   LLM     batch + realtime      — real, calculated LLM spend; the LLM cap governs THIS
#   remote  remote_compute        — real GPU/box compute; its OWN cap (resources.compute_exceeded)
#   sub     subscription          — the flat plan fee (real, but not per-call)
#   est     est_chat              — est-VALUE of subscription-covered usage; NOT billed, NEVER in a real total
LLM_USD_COLS = ("batch_usd", "realtime_usd")
# Forward-only additive columns: one added after the v5 schema shipped is ALTER-ADDed to an existing table
# (CREATE TABLE IF NOT EXISTS never adds a column to a table that already exists). Append new columns here.
# intent/actor are the FORENSIC pair carried from charges: WHAT the money bought · WHAT RAN IT.
_ADDITIVE_COLUMNS = (("intent", "TEXT"), ("actor", "TEXT"))
_KIND_TO_USD = {"batch": "batch_usd", "realtime": "realtime_usd",
                "est_chat": "est_chat_usd", "est-chat": "est_chat_usd", "estchat": "est_chat_usd",
                "remote": "remote_compute_usd", "remote_compute": "remote_compute_usd", "gpu": "remote_compute_usd",
                "subscription": "subscription_usd", "sub": "subscription_usd"}
_USD_TO_KIND = {"batch_usd": "batch", "realtime_usd": "realtime", "est_chat_usd": "est_chat",
                "remote_compute_usd": "remote_compute", "subscription_usd": "subscription"}
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
        # A TIMESTAMP WITH NO OFFSET IS UTC HERE, AND PYTHON DOES NOT KNOW THAT. fromisoformat returns a
        # NAIVE datetime for '2026-08-09T14:00:00', and .astimezone() on a naive value assumes LOCAL time —
        # so a UTC-canonical stamp was shifted by the host's offset and the charge landed on the wrong DAY,
        # and at a month boundary in the wrong PERIOD. Every row in this ledger is written UTC-canonical
        # (see _now_utc), so the missing offset is a formatting detail, not an unknown.
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=datetime.timezone.utc)
        if tzname and tzname != "UTC":
            from zoneinfo import ZoneInfo
            dt = dt.astimezone(ZoneInfo(tzname))
        return dt.date().isoformat(), dt.strftime("%Y-%m")
    except Exception:
        return (ts_iso or "")[:10], (ts_iso or "")[:7]


def dec(usd):
    """A money value → its canonical decimal string for storage, or None for absent. EXACT: Decimal(str(x))
    never introduces the binary-float error that `float(x)` would, so $0.00000026 round-trips unchanged."""
    if usd is None or usd == "":
        return None
    try:
        return str(Decimal(str(usd)))
    except (InvalidOperation, ValueError):
        return None


def to_dec(s):
    """A stored money string → Decimal (0 for absent/blank). The exact primitive every sum must go through."""
    if s in (None, ""):
        return Decimal(0)
    try:
        return Decimal(str(s))
    except (InvalidOperation, ValueError):
        return Decimal(0)


def to_usd(s):
    """A stored money value → float, for DISPLAY and legacy callers that expect a number. Exactness lives in
    the Decimal path (to_dec / dec_sum); this is the lossy edge where money meets a human-readable number."""
    return float(to_dec(s))


class _DecSum:
    """A SQLite aggregate that sums decimal-string money columns EXACTLY (Python Decimal), because SQL
    SUM() on a TEXT column coerces to float and reintroduces the error we moved off micros to avoid.
    Registered per-connection as `dec_sum`; returns a decimal string."""

    def __init__(self):
        self.acc = Decimal(0)

    def step(self, value):
        if value not in (None, ""):
            try:
                self.acc += Decimal(str(value))
            except (InvalidOperation, ValueError):
                pass

    def finalize(self):
        return str(self.acc)


_LIVE_SEQ = itertools.count()


def live_dedup_key(ts_iso):
    """A per-call UNIQUE dedup key for a live charge. `charges` had no dedup — every call was its own row —
    but `spend_events` dedups on id, so two identical calls (same provider/model/cost/second) would MERGE
    into one and money would be lost. Uniqueness = microsecond timestamp + process id + a monotonic counter,
    which no two calls in any process can collide on."""
    return f"live:{ts_iso}:{os.getpid()}:{next(_LIVE_SEQ)}"


class SpendLedger:
    """The one door to spend_events: SCRUD + queries + lifecycle/audit. Mutable until locked; integrity in spend_audit."""

    def __init__(self, db_path=None):
        self.db_path = db_path or config.db_path()
        self._conn = sqlite3.connect(self.db_path, timeout=10, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.create_aggregate("dec_sum", 1, _DecSum)   # exact decimal SUM for the TEXT money columns
        self._defer = False                              # bulk(): defer per-row commits (still audits every row)
        self._cols = self._ensure_schema()

    @contextlib.contextmanager
    def bulk(self):
        """Defer per-row commits for a bulk load (e.g. a one-time migration), committing once on exit — every row is
        still inserted + audited, only the fsync is batched. Use ONLY for trusted bulk ingest, not concurrent writers."""
        self._defer = True
        try:
            yield self
            self._conn.commit()
        except Exception:
            # ROLL BACK WHAT THE FAILED LOAD WROTE. Per-row commits are deferred here, so an exception left
            # the partial rows sitting in an OPEN transaction — uncommitted, but flushed by the next commit
            # from anywhere in the process. A bulk load that raised half way through therefore landed
            # silently, later, with nothing tying the rows to the load that failed. `finally` reset the
            # flag and let that happen.
            self._conn.rollback()
            raise
        finally:
            self._defer = False

    def flush(self):
        self._conn.commit()

    def _ensure_schema(self):
        for ddl in (_DDL_EVENTS, _DDL_AUDIT, _DDL_LOCKS):
            self._conn.execute(ddl)
        # Forward-only additive migration: CREATE TABLE IF NOT EXISTS leaves an existing table's columns as
        # they were, so a column added after v5 must be ALTER-ADDed here or record() silently drops it (it
        # only writes columns present in self._cols). Idempotent — skips a column that already exists.
        have = {r[1] for r in self._conn.execute("PRAGMA table_info(spend_events)")}
        for col, decl in _ADDITIVE_COLUMNS:
            if col not in have:
                self._conn.execute(f"ALTER TABLE spend_events ADD COLUMN {col} {decl}")
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
        # A PRIOR ROW WITH NO HASH IS A ROW SOMETHING WROTE AROUND THIS METHOD. Chaining onto None raised
        # TypeError and took the whole write down — so one out-of-band INSERT anywhere in the history
        # disabled auditing for every write after it. The chain restarts from "" instead; the break stays
        # visible to verify_audit_chain, which is where it belongs. Refusing to log is not integrity.
        prev_hash = (prev[0] if prev and prev[0] else "") or ""
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
            col = _KIND_TO_USD.get(kind)
            if not col:
                raise ValueError(f"unknown spend kind {kind!r}; expected batch | realtime | est_chat | remote | subscription")
            ev[col] = dec(usd)
        # A caller may set a category column directly (e.g. batch_usd=…). Normalise any to the canonical
        # decimal string so storage is uniform and sums are exact.
        for c in USD_COLS:
            if c in ev and ev[c] is not None:
                ev[c] = dec(ev[c])
        # A sub-micro real cost like $0.00000026 is now NON-zero (Decimal, not truncated micros) — the guard
        # fires only on a genuinely MISSING cost, which is the bug it was written to catch.
        nz = [c for c in USD_COLS if to_dec(ev.get(c)) != 0]
        # UNPRICED is the ONE legitimate zero-money row: the call HAPPENED (tokens real) but its price is
        # unknown, not zero. "$0" and "we can't price it" are different claims and only one is honest here, so
        # an explicit cost_basis='unpriced' row is allowed through with no money column — it stays out of every
        # $ total by summing to 0, and is findable by cost_basis for the "what we couldn't price" view. A row
        # with neither a cost NOR the unpriced flag is a genuinely missing cost, which is still a hard error.
        if not nz and ev.get("cost_basis") != "unpriced":
            raise ValueError("spend event has no cost in any money column")
        if not ev.get("dedup_key") and not ev.get("source"):
            raise ValueError("spend event needs a dedup_key or a source")
        ev.setdefault("currency", "USD")
        ev.setdefault("cost_type", _USD_TO_KIND[nz[0]] if len(nz) == 1 else None)
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
                    f"{ev.get('cost_type')} {sum((to_dec(ev.get(c)) for c in USD_COLS), Decimal(0))} USD", "ingested")
        if not self._defer:
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
    def _clone_for_correction(self, eid, kind_field, actor, reason, negate=False, overrides=None,
                              delta=False):
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
            for c in USD_COLS:
                ev[c] = str(-to_dec(row[c]))
        elif delta:
            # A CORRECTION POSTS THE DIFFERENCE, BECAUSE THE ORIGINAL ROW STAYS.
            #
            # This branch used to clone the original's amounts and let `overrides` replace some of them, so
            # the ledger ended up holding BOTH rows in full. MEASURED: an event of realtime=1,000,000 /
            # batch=250,000, adjusted to realtime=400,000, summed to realtime 1,400,000 and batch 500,000 —
            # the correction INFLATED the figure it was called to reduce, and doubled a column nobody
            # touched. reverse() had this right all along (it negates every column, so original + reversal
            # is exactly zero); adjust() was the same idea with the arithmetic left out.
            #
            # Every micro column gets a delta: the ones named in `changes` move to (new - old), and the ones
            # not named move by ZERO. A column omitted from a correction means "unchanged", and the only
            # posting that leaves a running total unchanged is 0 — not a second copy of it.
            for c in USD_COLS:
                want = (overrides or {}).get(c)
                ev[c] = str(to_dec(want) - to_dec(row[c])) if want is not None else "0"
            overrides = {k: v for k, v in (overrides or {}).items() if k not in USD_COLS}
        ev[kind_field] = eid
        ev["source"] = (row["source"] or "") + (":" + kind_field.split("_")[0])   # distinct id from the original
        ev.update(overrides or {})
        new_id = self.record(ev)                              # posts into the CURRENT open period
        self._audit(new_id, actor, "reverse" if negate else "adjust", kind_field, None, eid, reason)
        self._conn.commit()
        return new_id

    def audit(self, event_id, actor, pass_, field, old, new, reason):
        """Append one row to the hash-chained audit log — the ONLY supported way in from outside.

        It exists because two modules had already gone around it: budget.quarantine_charge and
        budget.reattribute_providers both INSERTed straight into spend_audit, leaving 796 rows with no
        hash and, until the fix above, breaking every audit write that came after them. A private method
        with no public equivalent is an invitation to write the INSERT by hand."""
        self._audit(event_id, actor, pass_, field, old, new, reason)
        self._conn.commit()

    def reverse(self, eid, actor="?", reason=""):
        """Post a reversing entry (negates the original) into the open period. The original stays untouched.

        REFUSES AN EVENT THAT HAS ALREADY BEEN ADJUSTED. A reversal negates the ORIGINAL amounts, so on an
        event carrying corrections it cancels the original and leaves the deltas behind: measured, an event
        of 1,000,000 adjusted down to 400,000 and then reversed summed to MINUS 600,000 — a negative figure
        for money that never existed, in the table the statement is built from.

        There is no single obviously-right answer (reverse the net? reverse and orphan the corrections?), so
        this refuses and says which entries are in the way rather than picking one and being quietly wrong
        about somebody's books. Reverse the adjustments first, or reverse the net explicitly."""
        adjs = [r[0] for r in self._conn.execute(
            "SELECT id FROM spend_events WHERE adjusts_id=?", (eid,)).fetchall()]
        if adjs:
            raise ValueError(
                f"event {eid!r} has {len(adjs)} adjustment(s) posted against it ({', '.join(map(str, adjs[:5]))}"
                f"{'...' if len(adjs) > 5 else ''}). Reversing it would negate the ORIGINAL amounts and leave "
                f"those corrections standing, producing a total for money that was never spent. Reverse the "
                f"adjustments first, or post an explicit reversal of the net.")
        return self._clone_for_correction(eid, "reverses_id", actor, reason, negate=True)

    def adjust(self, eid, changes, actor="?", reason=""):
        """Post a DELTA so the original and this entry SUM to the corrected figure. Linked via adjusts_id.

        `changes` states the values the event SHOULD have had; the entry posted here is the difference, so
        that `SUM(column)` across the pair equals what you asked for. The original is never touched — that
        is the whole point of a locked period — which is exactly why the correction must be a difference
        and not a second full copy.

        Non-amount fields in `changes` (project, org, tags) are attributes rather than quantities and are
        carried across as given; only the micro columns are differenced."""
        return self._clone_for_correction(eid, "adjusts_id", actor, reason, negate=False,
                                          overrides=changes, delta=True)

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
        sums = ", ".join(f"dec_sum({c})" for c in USD_COLS)      # EXACT decimal sum, not SQL SUM (float coercion)
        sel = (", ".join(cols) + ", " if cols else "") + sums + ", COUNT(*)"
        sql = f"SELECT {sel} FROM spend_events WHERE 1=1" + w + (" GROUP BY " + ", ".join(cols) if cols else "")

        def pack(row):
            vals = {c: to_dec(row[len(cols) + i]) for i, c in enumerate(USD_COLS)}   # exact Decimal per category
            billed = sum((vals[c] for c in BILLED_USD_COLS), Decimal(0))
            out = {c: float(vals[c]) for c in USD_COLS}
            out.update(billed_usd=float(billed), est_value_usd=float(vals["est_chat_usd"]), n=row[-1])
            return out
        rows = self._conn.execute(sql, args).fetchall()
        empty = {**{c: 0.0 for c in USD_COLS}, "billed_usd": 0.0, "est_value_usd": 0.0, "n": 0}
        if not cols:
            return pack(rows[0]) if rows and rows[0][-1] else empty
        return {(tuple(row[i] for i in range(len(cols))) if len(cols) > 1 else row[0]): pack(row) for row in rows}

    def by_repo(self, repo, since=None, until=None):
        return self.rollup(since=since, until=until, where={"repo": repo})

    # THE COUNTABLE FILTER, in ONE place. `charges` had this as the `countable_charges` view (exclude meta,
    # reconciliation markers, quarantined-impossible, and reconstructed-already-counted rows). On spend_events
    # the same four exclusions are flags, and this is the single definition the cap and every "what did we
    # spend" reader shares — so the cap's meaning can never drift from a report's.
    _COUNTABLE = ("COALESCE(is_meta,0)=0 AND COALESCE(reconciled,0)=0 "
                  "AND COALESCE(status,'') NOT IN ('void','reversed') "
                  "AND COALESCE(cost_basis,'') != 'reconstructed'")

    # est-value and subscription are excluded from "spend" the same way void is, but they are NOT subject to
    # the reconciled/reconstructed exclusions (those are LLM-metering concepts) — so each category names its
    # own filter rather than reusing _COUNTABLE, which is what keeps the five apart in the READ path too.
    _LIVE_FILTER = "COALESCE(is_meta,0)=0 AND COALESCE(status,'') NOT IN ('void','reversed')"

    def _cat_dec(self, cols, filt, since=None, until=None, where=None, **filters):
        """EXACT Decimal Σ over ONE category's column(s) under `filt` — the single machine behind every
        category accessor, so the split ('never sum est-value into real $') is structural, not a convention
        each caller has to remember. Returns a decimal string."""
        w, args = self._where(since, until, {**(where or {}), **filters})
        if filt:
            w += " AND " + filt
        sums = ", ".join(f"dec_sum({c})" for c in cols)
        r = self._conn.execute(f"SELECT {sums} FROM spend_events WHERE 1=1" + w, args).fetchone()
        return str(sum((to_dec(x) for x in r), Decimal(0)))

    def spent_dec(self, since=None, until=None, where=None, **filters):
        """EXACT Decimal total of COUNTABLE **LLM** spend — batch + realtime ONLY (the LLM cap's number and the
        honest 'what we spent on LLM'). NOT est-value, NOT GPU, NOT the subscription fee: those are separate
        categories with their own accessors (est_value_dec / remote_dec / subscription_dec) and must never be
        summed in here. Excludes meta, reconciliation markers, impossible-quarantined (void), and reconstructed
        rows — the same four the legacy countable_charges view excluded. Returns a decimal string."""
        return self._cat_dec(LLM_USD_COLS, self._COUNTABLE, since=since, until=until, where=where, **filters)

    def remote_dec(self, since=None, until=None, where=None, **filters):
        """EXACT Decimal total of real GPU/remote-compute spend — the number the COMPUTE cap governs, kept
        apart from the LLM cap on purpose (a box download can burn GPU-$ without any LLM call)."""
        return self._cat_dec(("remote_compute_usd",), self._COUNTABLE, since=since, until=until, where=where, **filters)

    def est_value_dec(self, since=None, until=None, where=None, **filters):
        """EXACT Decimal total of est-VALUE (est_chat_usd) — what subscription-covered usage WOULD have cost at
        API rates. Real money did NOT change hands for this, so it is reported on its own axis and is NEVER
        added into a real $ total or a cap."""
        return self._cat_dec(("est_chat_usd",), self._LIVE_FILTER, since=since, until=until, where=where, **filters)

    def subscription_dec(self, since=None, until=None, where=None, **filters):
        """EXACT Decimal total of the subscription FLAT FEE (subscription_usd) — real money, but a plan fee, not
        per-call spend, so it is its own line and not folded into the LLM cap."""
        return self._cat_dec(("subscription_usd",), self._LIVE_FILTER, since=since, until=until, where=where, **filters)

    def sum_dec(self, since=None, until=None, where=None, include_meta=True, include_void=False, **filters):
        """EXACT Decimal grand total across ALL FIVE cost columns, as a string. This is the RECONCILIATION
        primitive — compare it against a source Σ or provider truth. `sum_usd` is the float convenience for
        DISPLAY; the float() there is precisely the precision loss reconciliation must never suffer, which
        is why the two are different methods and this one is the source of truth.

        `include_void=True` is the MIGRATION-CONSERVATION mode: it drops the void exclusion so the sum covers
        every dollar that landed in the table, including quarantined-impossible rows (mapped to status=void).
        The cutover proves 'every charge dollar arrived' with this; reporting keeps void out (the default)."""
        w, args = self._where(since, until, {**(where or {}), **filters})
        if not include_meta:
            w += " AND COALESCE(is_meta,0)=0"
        if not include_void:
            w += " AND COALESCE(status,'') NOT IN ('void')"
        sums = ", ".join(f"dec_sum({c})" for c in USD_COLS)       # exact per-column decimal sums
        r = self._conn.execute(f"SELECT {sums} FROM spend_events WHERE 1=1" + w, args).fetchone()
        return str(sum((to_dec(x) for x in r), Decimal(0)))       # add the category totals exactly

    def sum_usd(self, since=None, until=None, where=None, include_meta=True, include_void=False, **filters):
        """Total USD for the filter, as a FLOAT — the display convenience. Exactness lives in `sum_dec`;
        this is the one lossy edge where money becomes a human-readable number. One implementation, wrapped."""
        return float(self.sum_dec(since=since, until=until, where=where, include_meta=include_meta,
                                  include_void=include_void, **filters))

    # ── integrity: the AUDIT LOG is hash-chained (not the live row) ──
    def verify_audit_chain(self, detail=False):
        """Recompute the spend_audit hash chain. (ok, first_bad_id|None), or the full report when detail=True.

        TWO DIFFERENT FAILURES, AND THEY MUST NOT READ THE SAME.
          unchained  a row with NO row_hash — something INSERTed straight into the table instead of going
                     through _audit. That is a bug in our own writer. Measured: 796 such rows, 697 written
                     by budget.reattribute_providers and 99 by quarantine_charge, both of which raw-INSERTed
                     an audit row while adding a forensic trail.
          tampered   a row whose hash is PRESENT and WRONG — the body was changed after it was written.
                     That is the event this table exists to catch.

        Reporting a bypassed writer as tampering cries wolf; reporting tampering as a bypassed writer is far
        worse. The hashes of unchained rows are NOT recomputed to make the chain look whole again: a
        tamper-evidence record that repairs itself is not evidence of anything."""
        prev, unchained, tampered = "", [], []
        for r in self._conn.execute("SELECT * FROM spend_audit ORDER BY id"):
            if not r["row_hash"]:
                unchained.append(r["id"])
                prev = ""                     # the next row legitimately restarts from empty
                continue
            body = json.dumps({k: r[k] for k in _AUDIT_FIELDS}, sort_keys=True, default=str)
            if hashlib.sha256((body + prev).encode()).hexdigest() != r["row_hash"]:
                tampered.append(r["id"])
            prev = r["row_hash"]
        if detail:
            return {"ok": not tampered, "tampered": tampered, "unchained": unchained,
                    "n_tampered": len(tampered), "n_unchained": len(unchained)}
        return (not tampered), (tampered[0] if tampered else None)

"""SpendLedger — the SINGLE gateway to spend data (an in-process context/data provider; MCP-style, not a server).

Every read/write of spend goes through this class. No consumer writes raw SQL against `spend_events` — the class owns
the schema and ALL queries/joins, returns typed dicts (JSON columns deserialised), and routes the agentic ATTRIBUTION
through one path. Deterministic SQL for queries; the LLM is used ONLY for attribution (meaning), recorded so re-runs
read it (repeatable).

Designed to financial-systems standards:
- **Money is integer micro-units** of `currency` (`*_micros`, ×1e6) — never float. Sums are exact.
- **Time** is UTC-canonical (`ts_utc`) with source-local (`tz`/`local_datetime`); the accounting `day`/`period` are
  derived in the REPORTING tz (`SPENDGUARD_REPORTING_TZ`, default UTC). Transaction date (`occurred_at`) is separate
  from posting date (`recorded_at`).
- **Append-only + correct-by-reversal** — never UPDATE a financial fact; supersede or post a reversal (`status`,
  `reverses_id`, `revision`).
- **Tamper-evident** — each row carries `row_hash = sha256(content + prev_hash)` (a hash chain; `verify_chain()`).
- **Self-contained record + link-ids** — snapshots cost/attribution/rates, plus `seg_id`/`call_id`/`conv_id`/
  `batch_id`/`model` links to source evidence.
"""
import os
import json
import sqlite3
import hashlib
import datetime
from . import config

SCHEMA_VERSION = 3

_DDL = """
CREATE TABLE IF NOT EXISTS spend_events (
  -- identity / dedup
  id            TEXT PRIMARY KEY,          -- deterministic evidence hash (re-record = no-op)
  dedup_key     TEXT,                      -- natural key (message-id / batch+custom_id) — kills double-count
  source        TEXT,                      -- gate | reconstruction | batch-api | gpu | est-chat | subscription
  content_hash  TEXT,
  schema_version INTEGER DEFAULT 3,
  -- time: UTC canonical + source-local; accounting day/period derived in the reporting tz
  ts_utc        TEXT,                      -- canonical UTC, tz-aware ISO-8601 (…+00:00) — ordering + math
  occurred_at   TEXT,                      -- TRANSACTION date: when the spend HAPPENED (UTC)
  recorded_at   TEXT,                      -- POSTING date: when we booked it (UTC)
  tz            TEXT,                      -- source zone (e.g. America/Los_Angeles)
  local_datetime TEXT,                     -- wall-clock at the source
  day           TEXT,                      -- accounting day (YYYY-MM-DD) in the reporting tz
  period        TEXT,                      -- accounting period (YYYY-MM) in the reporting tz
  eligibility_window TEXT,                 -- the period this spend is eligible to
  window_start  TEXT,
  window_end    TEXT,
  -- money: INTEGER micro-units of `currency` (never float; exact sums)
  currency      TEXT DEFAULT 'USD',
  batch_micros          INTEGER DEFAULT 0,
  realtime_micros       INTEGER DEFAULT 0,
  est_chat_micros       INTEGER DEFAULT 0, -- Claude Code/Codex plan usage (est-value axis; billed=0)
  remote_compute_micros INTEGER DEFAULT 0, -- vast.ai GPU
  subscription_micros   INTEGER DEFAULT 0, -- flat plan fee (Max/Pro), attributed proportionally
  cost_type     TEXT,                      -- batch|realtime|est_chat|remote_compute|subscription (filled as applicable)
  billed        INTEGER DEFAULT 1,
  is_meta       INTEGER DEFAULT 0,         -- spendguard's OWN spend — excluded from workload rollups
  cost_basis    TEXT,                      -- printed | estimated | gate-measured | provider-reconciled
  amount_confidence REAL,                  -- 0.0–1.0
  rate_in       REAL,                      -- $/1M tokens (price-book snapshot) — cost = tokens×rate auditable
  rate_out      REAL,
  fx_rate       REAL,                      -- to base currency (1.0 for USD)
  base_micros   INTEGER,                   -- amount in base currency micros
  -- provider / model
  provider      TEXT,
  model         TEXT,
  model_kind    TEXT,                      -- completion | embedding | image | gpu (kills embeddings-as-completions)
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
  projects      TEXT,                      -- JSON array (multi-project)
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
  seg_id        TEXT,                      -- → seg_attribution (the determination)
  call_id       TEXT,                      -- → calls (the gate's per-call record)
  cwd           TEXT,
  batch_id      TEXT,                      -- → provider batch
  from_message_ids  TEXT,                  -- JSON array
  prior_message_ids TEXT,                  -- JSON array
  post_message_ids  TEXT,                  -- JSON array
  script        TEXT,
  repo          TEXT,
  host          TEXT,
  prompt_hash   TEXT,
  prompt_snip   TEXT,
  output_snip   TEXT,
  evidence_uri  TEXT,                      -- transcript file:line / batch-result url
  -- attribution audit (why · what · how)
  attr_what     TEXT,
  attr_why      TEXT,
  attr_how      TEXT,                      -- cwd-match | lineage | llm | batch-map | gate-inline
  attr_reason   TEXT,
  attr_confidence REAL,                    -- 0.0–1.0
  attr_source   TEXT,
  attr_model    TEXT,
  attr_ts       TEXT,
  attr_version  TEXT,
  -- record provenance
  recorded_by   TEXT,                      -- which process wrote the row
  ingest_version TEXT,
  -- lifecycle / correction (append-only; correct by reversal, never UPDATE a fact)
  status        TEXT DEFAULT 'posted',     -- posted | reconciled | superseded | reversed | void
  revision      INTEGER DEFAULT 1,
  reverses_id   TEXT,                      -- this entry reverses that one
  superseded_by TEXT,
  -- reconciliation / close
  reconciled    INTEGER DEFAULT 0,
  reconciled_vs TEXT,                      -- provider | admin-dev-xcheck
  reconciled_at TEXT,
  reconciliation_id TEXT,
  gap_flag      TEXT,
  period_closed INTEGER DEFAULT 0,
  recon_marker  TEXT,
  -- integrity (tamper-evidence: hash chain)
  prev_hash     TEXT,
  row_hash      TEXT,
  -- quality / governance
  quality       TEXT,
  quality_src   TEXT,
  quality_conf  REAL,
  cache_hit     INTEGER DEFAULT 0,
  savings_cv    REAL,
  -- free
  tags          TEXT                       -- JSON array
)
"""

MICRO_COLS = ("batch_micros", "realtime_micros", "est_chat_micros", "remote_compute_micros", "subscription_micros")
BILLED_MICRO_COLS = ("batch_micros", "realtime_micros", "remote_compute_micros", "subscription_micros")  # est_chat = est-value
_KIND_TO_MICRO = {"batch": "batch_micros", "realtime": "realtime_micros",
                  "est_chat": "est_chat_micros", "est-chat": "est_chat_micros", "estchat": "est_chat_micros",
                  "remote": "remote_compute_micros", "remote_compute": "remote_compute_micros", "gpu": "remote_compute_micros",
                  "subscription": "subscription_micros", "sub": "subscription_micros"}
_MICRO_TO_KIND = {"batch_micros": "batch", "realtime_micros": "realtime", "est_chat_micros": "est_chat",
                  "remote_compute_micros": "remote_compute", "subscription_micros": "subscription"}
_JSON_COLS = ("projects", "from_message_ids", "prior_message_ids", "post_message_ids", "tags")
_EVIDENCE = ("source", "conv_id", "batch_id", "script", "model", "prompt_hash", "in_tok", "out_tok", "attr_what")
_HASH_FIELDS = ("id", "currency") + MICRO_COLS + ("org", "projects", "occurred_at", "source", "cost_type", "is_meta")
_INDEXES = ("org", "day", "period", "conv_id", "source", "batch_id", "dedup_key", "reconciled", "model_kind", "status")


def _now_utc():
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")


def _reporting_tz():
    return os.getenv("SPENDGUARD_REPORTING_TZ") or "UTC"


def _day_period(ts_iso, tzname):
    """Accounting day (YYYY-MM-DD) + period (YYYY-MM) for an instant, in the REPORTING tz — the boundary is undefined
    without a tz (23:30 PT is a different calendar day than UTC)."""
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
    """The one door to spend_events: SCRUD + every query + attribution. Money in integer micros; append-only; chained."""

    def __init__(self, db_path=None):
        self.db_path = db_path or config.db_path()
        self._conn = sqlite3.connect(self.db_path, timeout=10, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._cols = self._ensure_schema()

    def _ensure_schema(self):
        self._conn.execute(_DDL)
        for ix in _INDEXES:
            self._conn.execute(f"CREATE INDEX IF NOT EXISTS idx_se_{ix} ON spend_events({ix})")
        self._conn.commit()
        return [r[1] for r in self._conn.execute("PRAGMA table_info(spend_events)")]

    @staticmethod
    def _evidence_id(ev):
        key = ev.get("dedup_key") or "|".join(str(ev.get(k) or "") for k in _EVIDENCE)
        return hashlib.sha256(key.encode()).hexdigest()[:20]

    @staticmethod
    def _row_hash(ev, prev_hash):
        body = json.dumps({k: ev.get(k) for k in _HASH_FIELDS}, sort_keys=True, default=str)
        return hashlib.sha256((body + (prev_hash or "")).encode()).hexdigest()

    # ── C: create (validated, append-only, chained) ──
    def record(self, ev):
        """Validated write. (kind, usd) → the right micro column; UTC times + reporting-tz day/period; rate snapshot;
        deterministic id (dedup); hash-chained. Re-recording the same evidence is a no-op (no double-count, no chain
        advance). Returns the id."""
        ev = dict(ev)
        kind = (ev.pop("kind", None) or "").lower()
        usd = ev.pop("usd", None)
        if kind and usd is not None:
            col = _KIND_TO_MICRO.get(kind)
            if not col:
                raise ValueError(f"unknown spend kind {kind!r}; expected "
                                 "batch | realtime | est_chat | remote | subscription")
            ev[col] = micros(usd)
        for mc in MICRO_COLS:                                  # accept *_usd convenience for any cost type
            ucol = mc.replace("_micros", "_usd")
            if ucol in ev and ev.get(mc) is None:
                ev[mc] = micros(ev.pop(ucol))
        nz = [c for c in MICRO_COLS if int(ev.get(c) or 0)]
        if not nz:
            raise ValueError("spend event has no cost in any micros column "
                             "(batch/realtime/est_chat/remote_compute/subscription)")
        if not ev.get("dedup_key") and not ev.get("source"):
            raise ValueError("spend event needs a dedup_key or a source")
        ev.setdefault("currency", "USD")
        ev.setdefault("cost_type", _MICRO_TO_KIND[nz[0]] if len(nz) == 1 else None)
        # time: UTC canonical + source-local + reporting-tz accounting day/period (from the TRANSACTION date)
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
        # rate snapshot from the price book (cost = tokens × rate auditable)
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
        ev.setdefault("status", "posted")
        ev.setdefault("revision", 1)
        ev["id"] = ev.get("id") or self._evidence_id(ev)
        if self._conn.execute("SELECT 1 FROM spend_events WHERE id=?", (ev["id"],)).fetchone():
            return ev["id"]                                   # already booked — no double-count, chain not advanced
        for c in MICRO_COLS:                                  # normalise hashed defaults so record() == verify_chain()
            ev.setdefault(c, 0)
        ev.setdefault("is_meta", 0)
        for jc in _JSON_COLS:                                 # serialise JSON BEFORE hashing → identical content both sides
            if jc in ev and not isinstance(ev.get(jc), (str, type(None))):
                ev[jc] = json.dumps(ev[jc])
        prev = self._conn.execute("SELECT row_hash FROM spend_events ORDER BY rowid DESC LIMIT 1").fetchone()
        ev["prev_hash"] = prev[0] if prev else ""
        ev["row_hash"] = self._row_hash(ev, ev["prev_hash"])
        cols = [c for c in self._cols if c in ev]
        self._conn.execute(
            f"INSERT INTO spend_events ({','.join(cols)}) VALUES ({','.join('?' for _ in cols)})",
            [ev.get(c) for c in cols])
        self._conn.commit()
        return ev["id"]

    # ── R: read ──
    def get(self, eid):
        r = self._conn.execute("SELECT * FROM spend_events WHERE id=?", (eid,)).fetchone()
        return self._row(r) if r else None

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

    # ── rollup: the cost breakdown, billed vs est-value split (computed ONCE; exact integer micros + usd) ──
    def rollup(self, group_by=None, since=None, until=None, where=None, include_meta=False):
        """{group: {<cost>_micros, <cost>_usd, billed_micros, billed_usd, est_value_micros, est_value_usd, n}}.
        group_by=None → one totals dict. billed = batch+realtime+remote_compute+subscription (REAL $); est_value =
        est_chat (separate, never summed). is_meta excluded unless include_meta. Exact integer micros; usd is display."""
        cols = [group_by] if isinstance(group_by, str) else list(group_by or [])
        for g in cols:
            if g not in self._cols:
                raise ValueError(f"unknown group_by column {g!r}")
        w, args = self._where(since, until, where)
        if not include_meta:
            w += " AND COALESCE(is_meta,0)=0"
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
        """Repo-scoped rollup — charm = ONLY charm's events (so e.g. $0 remote when it ran no vast.ai). A filter,
        so the 'charm shows global $1,225 remote' scoping bug cannot recur."""
        return self.rollup(since=since, until=until, where={"repo": repo})

    # ── integrity: tamper-evidence ──
    def verify_chain(self):
        """Recompute the hash chain in insertion order; return (ok, first_bad_id|None). Proves no row was altered."""
        prev = ""
        for r in self._conn.execute("SELECT * FROM spend_events ORDER BY rowid"):
            ev = {k: r[k] for k in r.keys()}                   # RAW stored values (JSON cols stay as stored strings)
            if self._row_hash(ev, prev) != r["row_hash"]:
                return False, r["id"]
            prev = r["row_hash"]
        return True, None

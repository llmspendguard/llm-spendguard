"""SpendLedger — the SINGLE gateway to spend data (an in-process context/data provider; MCP-style, not a server).

Every read/write of spend goes through this class. No consumer writes raw SQL against `spend_events` — the class owns
the schema and ALL queries/joins, returns typed dicts (JSON columns deserialised), and routes the agentic ATTRIBUTION
through one path so every event is attributed + recorded the same way. Deterministic SQL for queries; the LLM is used
ONLY for attribution (meaning), and that determination is RECORDED so re-runs read it (repeatable, not re-guessed).

ONE forensic row per spend EVENT. The four cost types are SEPARATE columns (batch / realtime / est_chat / remote) so a
rollup is `SUM(col)` — never a leaky `GROUP BY kind`. Identity + lineage + the attribution audit make every $ explainable.

Built foundation-first: schema → SCRUD → queries → attribution, each reviewed/tested/validated before any consumer
hooks up. Enforced by tests/test_ledger.py (round-trip, dedup, cost-routing) and a guard against raw SQL elsewhere.
"""
import os
import json
import sqlite3
import hashlib
import datetime
from . import config

# ── the forensic schema — explicit + reviewable ──────────────────────────────────────────────────────
_DDL = """
CREATE TABLE IF NOT EXISTS spend_events (
  -- identity / dedup
  id            TEXT PRIMARY KEY,          -- deterministic evidence hash (re-record = no-op)
  dedup_key     TEXT,                      -- natural key (message-id / batch+custom_id) — kills double-count
  source        TEXT,                      -- gate | reconstruction | batch-api | gpu | est-chat
  content_hash  TEXT,
  -- time
  ts            TEXT,
  day           TEXT,
  window_start  TEXT,
  window_end    TEXT,
  eligibility_window TEXT,                 -- the period this spend is eligible to (per-period split)
  -- cost: FOUR separate columns (a row has ONE non-zero; rollup = SUM(col))
  batch_usd     REAL DEFAULT 0,
  realtime_usd  REAL DEFAULT 0,
  est_chat_usd  REAL DEFAULT 0,            -- Claude Code/Codex plan usage (billed=0)
  remote_usd    REAL DEFAULT 0,
  billed        INTEGER DEFAULT 1,
  cost_basis    TEXT,                      -- printed | estimated | gate-measured | provider-reconciled
  amount_confidence INTEGER,
  rate_in       REAL,
  rate_out      REAL,
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
  -- lineage / evidence
  conv_id       TEXT,
  seg_id        TEXT,
  cwd           TEXT,
  batch_id      TEXT,
  from_message_ids  TEXT,                  -- JSON array
  prior_message_ids TEXT,                  -- JSON array
  post_message_ids  TEXT,                  -- JSON array
  script        TEXT,
  repo          TEXT,
  host          TEXT,
  prompt_hash   TEXT,
  prompt_snip   TEXT,
  output_snip   TEXT,
  -- attribution audit (why · what · how)
  attr_what     TEXT,
  attr_why      TEXT,
  attr_how      TEXT,                       -- cwd-match | lineage | llm | batch-map | gate-inline
  attr_reason   TEXT,
  attr_confidence INTEGER,
  attr_source   TEXT,
  attr_model    TEXT,
  attr_ts       TEXT,
  attr_version  TEXT,
  -- reconciliation / lifecycle
  reconciled    INTEGER DEFAULT 0,
  reconciled_vs TEXT,                       -- provider | admin-dev-xcheck
  gap_flag      TEXT,
  superseded_by TEXT,
  recon_marker  TEXT,
  -- quality / governance
  quality       TEXT,
  quality_src   TEXT,
  quality_conf  INTEGER,
  cache_hit     INTEGER DEFAULT 0,
  savings_cv    REAL,
  -- free
  tags          TEXT                        -- JSON array
)
"""

COST_COLS = ("batch_usd", "realtime_usd", "est_chat_usd", "remote_usd")
_KIND_TO_COL = {"batch": "batch_usd", "realtime": "realtime_usd",
                "est_chat": "est_chat_usd", "est-chat": "est_chat_usd", "estchat": "est_chat_usd",
                "remote": "remote_usd", "remote_compute": "remote_usd", "gpu": "remote_usd"}
_JSON_COLS = ("projects", "from_message_ids", "prior_message_ids", "post_message_ids", "tags")
# stable evidence signature for the deterministic id (NO wall-clock ts → re-record dedups)
_EVIDENCE = ("source", "conv_id", "batch_id", "script", "model", "prompt_hash", "in_tok", "out_tok", "attr_what")
_INDEXES = ("org", "day", "conv_id", "source", "batch_id", "dedup_key", "reconciled", "model_kind")


class SpendLedger:
    """The one door to spend_events: SCRUD + every query + attribution, returning dicts/JSON. Consumers never SQL."""

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
        return [r[1] for r in self._conn.execute("PRAGMA table_info(spend_events)")]   # the real column set

    @staticmethod
    def _evidence_id(ev):
        """Deterministic id from a stable evidence signature (no wall-clock) — re-recording the same event is a
        no-op, which is how double-count is killed structurally. A caller can pass an explicit dedup_key for control."""
        key = ev.get("dedup_key") or "|".join(str(ev.get(k) or "") for k in _EVIDENCE)
        return hashlib.sha256(key.encode()).hexdigest()[:20]

    # ── C: create (validated write) ──
    def record(self, ev):
        """Validated write. A (kind, usd) pair routes to exactly one cost column; JSON fields serialise; idempotent
        on the deterministic id. Returns the id. Re-recording the same evidence inserts nothing (no double-count)."""
        ev = dict(ev)
        kind = (ev.pop("kind", None) or "").lower()
        usd = ev.pop("usd", None)
        if kind and usd is not None:
            col = _KIND_TO_COL.get(kind)
            if not col:
                raise ValueError(f"unknown spend kind {kind!r}; expected batch | realtime | est_chat | remote")
            ev[col] = float(usd)
        if not any(float(ev.get(c) or 0) for c in COST_COLS):
            raise ValueError("spend event has no cost in any of batch/realtime/est_chat/remote")
        if not ev.get("dedup_key") and not ev.get("source"):
            raise ValueError("spend event needs a dedup_key or a source")
        ev.setdefault("ts", datetime.datetime.now().isoformat(timespec="seconds"))
        ev.setdefault("day", (ev["ts"] or "")[:10])
        ev["id"] = ev.get("id") or self._evidence_id(ev)
        for jc in _JSON_COLS:
            if jc in ev and not isinstance(ev.get(jc), (str, type(None))):
                ev[jc] = json.dumps(ev[jc])
        cols = [c for c in self._cols if c in ev]
        self._conn.execute(
            f"INSERT OR IGNORE INTO spend_events ({','.join(cols)}) VALUES ({','.join('?' for _ in cols)})",
            [ev.get(c) for c in cols])
        self._conn.commit()
        return ev["id"]

    # ── R: read ──
    def get(self, eid):
        """One event → dict (JSON columns deserialised), or None."""
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
        """Flexible read → list of event dicts. `where` = exact-match column filters (org, team, source, provider,
        model_kind, repo, reconciled, …); since/until filter on day."""
        w, args = self._where(since, until, where)
        sql = "SELECT * FROM spend_events WHERE 1=1" + w + " ORDER BY day"
        if limit:
            sql += f" LIMIT {int(limit)}"
        return [self._row(r) for r in self._conn.execute(sql, args).fetchall()]

    # ── rollup: the 4-column breakdown, billed vs est-value split (the hard cost rule, computed ONCE) ──
    def rollup(self, group_by=None, since=None, until=None, where=None):
        """{group: {batch_usd, realtime_usd, est_chat_usd, remote_usd, billed, est_value, n}}. group_by=None → one
        totals dict. billed = batch+realtime+remote (REAL $); est_value = est_chat (the SEPARATE axis, NEVER summed in).
        Subscription is a flat plan fee added at the DISPLAY layer, not per event."""
        cols = [group_by] if isinstance(group_by, str) else list(group_by or [])
        for g in cols:
            if g not in self._cols:
                raise ValueError(f"unknown group_by column {g!r}")
        w, args = self._where(since, until, where)
        sel = (", ".join(cols) + ", " if cols else "") + (
            "ROUND(SUM(batch_usd),4), ROUND(SUM(realtime_usd),4), ROUND(SUM(est_chat_usd),4), "
            "ROUND(SUM(remote_usd),4), COUNT(*)")
        sql = f"SELECT {sel} FROM spend_events WHERE 1=1" + w + (" GROUP BY " + ", ".join(cols) if cols else "")

        def pack(row):
            b, r, e, m, n = (row[-5] or 0), (row[-4] or 0), (row[-3] or 0), (row[-2] or 0), row[-1]
            return {"batch_usd": b, "realtime_usd": r, "est_chat_usd": e, "remote_usd": m,
                    "billed": round(b + r + m, 4), "est_value": e, "n": n}
        rows = self._conn.execute(sql, args).fetchall()
        if not cols:
            return pack(rows[0]) if rows and rows[0][-1] else \
                {"batch_usd": 0, "realtime_usd": 0, "est_chat_usd": 0, "remote_usd": 0, "billed": 0, "est_value": 0, "n": 0}
        return {(tuple(row[i] for i in range(len(cols))) if len(cols) > 1 else row[0]): pack(row) for row in rows}

    def by_repo(self, repo, since=None, until=None):
        """Repo-scoped 4-column rollup — the receipt's per-repo view. charm = ONLY charm's events (so e.g. $0 remote
        when charm ran no vast.ai). The scoping bug ('charm shows global $1,225 remote') can't recur: it's a filter."""
        return self.rollup(since=since, until=until, where={"repo": repo})

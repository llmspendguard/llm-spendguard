"""Insights (confidence-scored learnings) + the temporal learning graph (provenance + evolution).

Both share the SQLite db (config.db_path()). The graph captures how the deterministic evidence
evolved and how the conversation/scripts drove it: nodes (run/decision/conversation_event/
script_version/insight/outcome) + timestamped causal edges. Layer 1 seeds `run` nodes from backfill;
conversation/decision/script nodes + edges come in Layer 2 (mining).
"""
import sqlite3, datetime, threading, json
from . import config

_conn = None
_lock = threading.RLock()


def _db():
    global _conn
    if _conn is None:
        with _lock:
            if _conn is None:
                c = sqlite3.connect(config.db_path(), timeout=10, check_same_thread=False)
                c.execute("PRAGMA journal_mode=WAL")
                c.execute("""CREATE TABLE IF NOT EXISTS insights(
                    id TEXT PRIMARY KEY, ts TEXT, intent TEXT, lesson TEXT,
                    evidence TEXT, source TEXT, confidence REAL)""")
                # ── living-insight + applicability columns (migrate older dbs) ──
                # A bare metric isn't reasoning-able or shareable: an insight is a CONDITIONAL rule
                # (IF context THEN action BECAUSE mechanism) that carries the regime it holds in, the
                # quality basis behind it, and a lifecycle so it can be re-validated as data grows.
                have = {r[1] for r in c.execute("PRAGMA table_info(insights)").fetchall()}
                for col, decl in (
                    ("task_class", "TEXT"),    # classification | extraction | generation | judging | embedding | reasoning
                    ("regime", "TEXT"),        # bulk | interactive
                    ("output_shape", "TEXT"),  # short-structured | short-text | long-form
                    ("scale", "TEXT"),         # order-of-magnitude bucket (10s / 1000s / 100k+)
                    ("condition", "TEXT"),     # the IF
                    ("action", "TEXT"),        # the THEN
                    ("mechanism", "TEXT"),     # the BECAUSE (why it holds)
                    ("quality_basis", "TEXT"), # judged(n) | used | unverified
                    ("n_observations", "INTEGER"),
                    ("status", "TEXT"),        # candidate | active | superseded | refuted
                    ("support", "REAL"),       # corroborating weight
                    ("contradiction", "REAL"), # contradicting weight
                    ("last_validated", "TEXT"),
                    ("version", "INTEGER"),
                    ("scope", "TEXT")):        # private | shareable
                    if col not in have:
                        c.execute(f"ALTER TABLE insights ADD COLUMN {col} {decl}")
                c.execute("""CREATE TABLE IF NOT EXISTS graph_nodes(
                    id TEXT PRIMARY KEY, ts TEXT, type TEXT, label TEXT, attrs TEXT)""")
                c.execute("""CREATE TABLE IF NOT EXISTS graph_edges(
                    src TEXT, dst TEXT, rel TEXT, ts TEXT, attrs TEXT)""")
                # ── seg_attribution: the AGENTIC per-subconversation attribution decisions, recorded so we NEVER
                #    redo / re-pay for them. source ∈ prior(free, recomputable — not stored) | llm | human(final, wins).
                #    The convergence loop re-runs only rows that are absent / not-llm-or-human / below confidence τ. ──
                c.execute("""CREATE TABLE IF NOT EXISTS seg_attribution(
                    seg_id TEXT PRIMARY KEY, content_hash TEXT, sid TEXT, cwd TEXT, prompt TEXT,
                    project TEXT, org TEXT, team TEXT, confidence INTEGER,
                    source TEXT, model TEXT, ts TEXT, batch_ids TEXT)""")
                c.execute("CREATE INDEX IF NOT EXISTS idx_ins_intent ON insights(intent)")
                c.execute("CREATE INDEX IF NOT EXISTS idx_gn_type ON graph_nodes(type)")
                c.execute("CREATE INDEX IF NOT EXISTS idx_seg_source ON seg_attribution(source)")
                c.commit()
                _conn = c
    return _conn


def _uid():
    import uuid
    return uuid.uuid4().hex[:16]


def _now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")


# ── insights (conditional, context-rich, lifecycle-tracked) ──
_CTX = ("task_class", "regime", "output_shape", "scale", "condition", "action", "mechanism",
        "quality_basis", "n_observations", "scope")


def add_insight(intent, lesson, evidence=None, source="manual", confidence=0.5, ctx=None,
                status="candidate", scope="private"):
    """Record a learning. `ctx` carries the applicability (task_class/regime/output_shape/scale) and the
    structured rule (condition/action/mechanism) + quality_basis/n_observations — what makes it reusable
    and (when scrubbed) shareable. New insights start as 'candidate' until validated against the corpus."""
    iid = _uid()
    ctx = dict(ctx or {})
    cols = ["id", "ts", "intent", "lesson", "evidence", "source", "confidence",
            "status", "support", "contradiction", "last_validated", "version", "scope"] + list(_CTX)
    ctx.setdefault("scope", scope)
    vals = [iid, _now(), intent, lesson, evidence, source, float(confidence),
            status, 0.0, 0.0, _now(), 1, ctx.get("scope", scope)] + [ctx.get(k) for k in _CTX]
    with _lock:
        _db().execute(f"INSERT INTO insights ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})", vals)
        _db().commit()
    return iid


# status priority for ranking (active learnings beat unproven candidates; refuted sinks)
_STATUS_RANK = "CASE status WHEN 'active' THEN 3 WHEN 'candidate' THEN 2 WHEN 'superseded' THEN 1 ELSE 0 END"


def insights(intent=None, min_conf=0.0, include_refuted=False, scope=None):
    where = ["confidence >= ?"]
    args = [min_conf]
    if intent:
        where.append("intent = ?"); args.append(intent)
    if not include_refuted:
        where.append("(status IS NULL OR status != 'refuted')")
    if scope:
        where.append("scope = ?"); args.append(scope)
    with _lock:
        return _db().execute(
            f"SELECT intent, lesson, source, confidence, evidence FROM insights "
            f"WHERE {' AND '.join(where)} ORDER BY {_STATUS_RANK} DESC, confidence DESC", args).fetchall()


def insights_full(intent=None, include_refuted=False):
    """Rich rows incl. applicability + lifecycle — for validate / review / export."""
    where = [] if include_refuted else ["(status IS NULL OR status != 'refuted')"]
    args = []
    if intent:
        where.append("intent = ?"); args.append(intent)
    w = ("WHERE " + " AND ".join(where)) if where else ""
    with _lock:
        cur = _db().execute(
            f"SELECT id,intent,lesson,evidence,source,confidence,status,support,contradiction,"
            f"last_validated,version,scope,{','.join(_CTX)} FROM insights {w} "
            f"ORDER BY {_STATUS_RANK} DESC, confidence DESC", args)
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]


def update_insight(iid, **fields):
    """Update lifecycle/confidence fields (used by the validate pass)."""
    if not fields:
        return
    keys = ", ".join(f"{k}=?" for k in fields)
    with _lock:
        _db().execute(f"UPDATE insights SET {keys} WHERE id=?", list(fields.values()) + [iid])
        _db().commit()


# ── temporal learning graph ──
def add_node(type, label, attrs=None, ts=None, id=None):
    nid = id or _uid()
    with _lock:
        _db().execute("INSERT OR REPLACE INTO graph_nodes VALUES (?,?,?,?,?)",
                      (nid, ts or _now(), type, label, json.dumps(attrs or {})))
        _db().commit()
    return nid


def add_edge(src, dst, rel, ts=None, attrs=None):
    with _lock:
        _db().execute("INSERT INTO graph_edges VALUES (?,?,?,?,?)",
                      (src, dst, rel, ts or _now(), json.dumps(attrs or {})))
        _db().commit()


def graph_stats():
    with _lock:
        nodes = _db().execute("SELECT type, COUNT(*) FROM graph_nodes GROUP BY type ORDER BY 2 DESC").fetchall()
        edges = _db().execute("SELECT rel, COUNT(*) FROM graph_edges GROUP BY rel ORDER BY 2 DESC").fetchall()
    return nodes, edges

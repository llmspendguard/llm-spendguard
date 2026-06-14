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
                c.execute("""CREATE TABLE IF NOT EXISTS graph_nodes(
                    id TEXT PRIMARY KEY, ts TEXT, type TEXT, label TEXT, attrs TEXT)""")
                c.execute("""CREATE TABLE IF NOT EXISTS graph_edges(
                    src TEXT, dst TEXT, rel TEXT, ts TEXT, attrs TEXT)""")
                c.execute("CREATE INDEX IF NOT EXISTS idx_ins_intent ON insights(intent)")
                c.execute("CREATE INDEX IF NOT EXISTS idx_gn_type ON graph_nodes(type)")
                c.commit()
                _conn = c
    return _conn


def _uid():
    import uuid
    return uuid.uuid4().hex[:16]


def _now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")


# ── insights ──
def add_insight(intent, lesson, evidence=None, source="manual", confidence=0.5):
    iid = _uid()
    with _lock:
        _db().execute("INSERT INTO insights VALUES (?,?,?,?,?,?,?)",
                      (iid, _now(), intent, lesson, evidence, source, float(confidence)))
        _db().commit()
    return iid


def insights(intent=None, min_conf=0.0):
    where = ["confidence >= ?"]
    args = [min_conf]
    if intent:
        where.append("intent = ?")
        args.append(intent)
    with _lock:
        return _db().execute(
            f"SELECT intent, lesson, source, confidence, evidence FROM insights "
            f"WHERE {' AND '.join(where)} ORDER BY confidence DESC", args).fetchall()


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

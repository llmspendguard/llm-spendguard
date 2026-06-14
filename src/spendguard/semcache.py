"""Semantic response cache (opt-in) — serve duplicate / near-duplicate prompts from a local store,
avoiding the LLM call entirely. The one lever here that REDUCES spend rather than just measuring it.

Two tiers, cheapest/safest first (borrowed from GPTCache/Portkey, re-implemented — not a hard dep):
  EXACT     prompt-hash match — zero risk, free; huge on repetitive batch workloads (same concept
            re-typed across chunks).
  SEMANTIC  embedding cosine ≥ threshold — opt-in, conservative (high threshold); catches near-dupes.
            Each miss embeds once (text-embedding-3-small, cheap, gate-metered); a HIT avoids the costly
            generation. Only worth it when duplicates are common — measure with `spendguard cache-stats`.

Wrap your call (no auto-hijack of the gate — explicit + safe):
    out = semcache.cached_call(lambda p: client_call(p), prompt, model, threshold=0.0)
threshold=0.0 → exact-only (default, zero risk). >0 → also semantic. Returns the (possibly cached) text.
"""
import os, sqlite3, struct, hashlib, threading, datetime
from . import config

_conn = None
_lock = threading.RLock()
_stats = {"exact": 0, "semantic": 0, "miss": 0, "saved": 0.0}


def _db():
    global _conn
    if _conn is None:
        with _lock:
            if _conn is None:
                c = sqlite3.connect(config.db_path(), timeout=10, check_same_thread=False)
                c.execute("PRAGMA journal_mode=WAL")
                c.execute("""CREATE TABLE IF NOT EXISTS semcache(
                    id TEXT PRIMARY KEY, ts TEXT, model TEXT, prompt_hash TEXT, prompt TEXT,
                    output TEXT, emb BLOB)""")
                c.execute("CREATE INDEX IF NOT EXISTS idx_sc_hash ON semcache(model, prompt_hash)")
                c.commit()
                _conn = c
    return _conn


def _hash(s):
    return hashlib.sha256((s or "").encode("utf-8", "ignore")).hexdigest()[:32]


def _pack(vec):
    return struct.pack(f"{len(vec)}f", *vec)


def _unpack(b):
    return list(struct.unpack(f"{len(b)//4}f", b)) if b else None


def _embed(text):
    """text-embedding-3-small (cheap; gate-metered). Returns a vector or None on failure."""
    try:
        from openai import OpenAI
        r = OpenAI(api_key=config.api_key("OPENAI_API_KEY")).embeddings.create(
            model="text-embedding-3-small", input=[text[:8000]])
        return r.data[0].embedding
    except Exception:
        return None


def _cos(a, b):
    dot = sum(x * y for x, y in zip(a, b))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(y * y for y in b) ** 0.5
    return (dot / (na * nb)) if na and nb else 0.0


def get(prompt, model, threshold=0.0):
    """Cached output for an exact (and, if threshold>0, semantic) match — else None."""
    h = _hash(prompt)
    with _lock:
        row = _db().execute("SELECT output FROM semcache WHERE model=? AND prompt_hash=? LIMIT 1",
                            (model, h)).fetchone()
    if row:
        _stats["exact"] += 1
        return row[0]
    if threshold and threshold > 0:
        qv = _embed(prompt)
        if qv:
            with _lock:
                rows = _db().execute("SELECT output, emb FROM semcache WHERE model=? AND emb IS NOT NULL",
                                     (model,)).fetchall()
            best, bout = threshold, None
            for out, emb in rows:
                v = _unpack(emb)
                if v and len(v) == len(qv):
                    s = _cos(qv, v)
                    if s >= best:
                        best, bout = s, out
            if bout is not None:
                _stats["semantic"] += 1
                return bout
    _stats["miss"] += 1
    return None


def put(prompt, model, output, store_embedding=False):
    import uuid
    emb = _pack(_embed(prompt) or []) if store_embedding else None
    with _lock:
        _db().execute("INSERT OR REPLACE INTO semcache VALUES (?,?,?,?,?,?,?)",
                      (uuid.uuid4().hex[:16], datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
                       model, _hash(prompt), prompt[:2000], output, emb))
        _db().commit()


def cached_call(fn, prompt, model, threshold=0.0, est_cost=0.0):
    """Return cached output on hit (free), else fn(prompt) → store → return. fn returns the text output."""
    hit = get(prompt, model, threshold=threshold)
    if hit is not None:
        _stats["saved"] += est_cost
        return hit
    out = fn(prompt)
    put(prompt, model, out, store_embedding=(threshold and threshold > 0))
    return out


def stats():
    with _lock:
        n = _db().execute("SELECT COUNT(*), COUNT(emb) FROM semcache").fetchone()
    return {**_stats, "cached_entries": n[0], "with_embedding": n[1]}


def cmd(argv=None):
    s = stats()
    total = s["exact"] + s["semantic"] + s["miss"]
    hr = (s["exact"] + s["semantic"]) / total if total else 0
    print("semantic cache — opt-in response cache (avoids the call on duplicates)")
    print(f"  entries: {s['cached_entries']:,} ({s['with_embedding']:,} with embeddings)")
    print(f"  this process: {s['exact']} exact hits · {s['semantic']} semantic hits · {s['miss']} misses"
          f"  → hit-rate {100*hr:.0f}%   est saved ${s['saved']:.4f}")
    print("  use: semcache.cached_call(fn, prompt, model, threshold=0.97) — threshold 0 = exact-only (zero risk).")
    return 0

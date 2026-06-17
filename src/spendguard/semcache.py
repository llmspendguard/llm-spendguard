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
import sqlite3, struct, hashlib, threading, datetime
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
        from . import guard
        guard.record_saving("cache", est_cost)          # guarded: the API cost this cache hit avoided
        return hit
    out = fn(prompt)
    put(prompt, model, out, store_embedding=(threshold and threshold > 0))
    return out


def stats():
    with _lock:
        n = _db().execute("SELECT COUNT(*), COUNT(emb) FROM semcache").fetchone()
    return {**_stats, "cached_entries": n[0], "with_embedding": n[1]}


def _line_prompt(o):
    body = o.get("body") or o.get("params") or o
    for m in (body.get("messages") or []):
        if isinstance(m, dict) and m.get("role") == "user":
            c = m.get("content")
            return c if isinstance(c, str) else _json_dumps(c)
    return o.get("prompt") or ""


def _json_dumps(x):
    import json
    return json.dumps(x)


def dedup_jsonl(input_path, out_path, model="*", map_path=None):
    """Collapse a batch jsonl: drop within-batch duplicate prompts AND prompts already in the persistent
    cache (already processed in a prior run/retry — the real saver). Writes the unique requests to
    out_path. NOTE: on FRESH unique-prompt workloads this is ~0%; its win is re-runs/retries/overlap."""
    import json
    total = kept_n = within_dup = cache_hit = 0
    seen = set()
    dup_map = {}
    kept_ids = []
    with open(input_path, errors="ignore") as fin, open(out_path, "w") as fout:
        for ln in fin:
            ln = ln.strip()
            if not ln:
                continue
            try:
                o = json.loads(ln)
            except Exception:
                fout.write(ln + "\n"); kept_n += 1; total += 1; continue
            total += 1
            cid = o.get("custom_id") or f"i{total}"
            h = _hash(_line_prompt(o))
            if h in seen:
                within_dup += 1
                dup_map.setdefault(h, []).append(cid)
                continue
            with _lock:
                row = _db().execute("SELECT 1 FROM semcache WHERE model IN (?,?) AND prompt_hash=? LIMIT 1",
                                    (model, "*", h)).fetchone()
            if row:
                cache_hit += 1
                continue
            seen.add(h); dup_map[h] = [cid]; kept_ids.append(cid)
            fout.write(ln + "\n"); kept_n += 1
    if map_path:
        import json as _j
        _j.dump({"kept": kept_ids, "groups": list(dup_map.values())}, open(map_path, "w"))
    ratio = total / kept_n if kept_n else 1.0
    print(f"dedup — {total:,} requests → {kept_n:,} to submit "
          f"({within_dup:,} within-batch dup, {cache_hit:,} already-cached) = {ratio:.2f}x, "
          f"{100*(1-kept_n/total) if total else 0:.0f}% fewer calls")
    if ratio < 1.05:
        print("  (≈no duplication here — fresh unique prompts; the win comes on re-runs/retries/overlap.)")
    return dict(total=total, kept=kept_n, within_dup=within_dup, cache_hit=cache_hit, ratio=ratio)


def populate_jsonl(input_path, results_path, model="*"):
    """After a batch completes, store prompt→output so a future dedup skips those items (free re-runs)."""
    import json
    prompts = {}
    for ln in open(input_path, errors="ignore"):
        ln = ln.strip()
        if not ln:
            continue
        try:
            o = json.loads(ln)
        except Exception:
            continue
        prompts[o.get("custom_id")] = _line_prompt(o)
    n = 0
    for ln in open(results_path, errors="ignore"):
        ln = ln.strip()
        if not ln:
            continue
        try:
            o = json.loads(ln)
        except Exception:
            continue
        cid = o.get("custom_id")
        body = (o.get("response") or {}).get("body") or {}
        out = ((body.get("choices") or [{}])[0].get("message") or {}).get("content")
        if cid in prompts and out:
            put(prompts[cid], model, out)
            n += 1
    print(f"populated {n} prompt→output pairs into the cache — future dedup will skip them (free re-runs).")
    return n


def dedup_main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(prog="spendguard dedup")
    ap.add_argument("--input", required=True, help="batch .jsonl to dedup")
    ap.add_argument("--out", required=True, help="write the unique requests here")
    ap.add_argument("--map", help="write the id-grouping map here (json)")
    ap.add_argument("--model", default="*", help="cache scope (default * = any)")
    a = ap.parse_args(argv)
    dedup_jsonl(a.input, a.out, model=a.model, map_path=a.map)
    return 0


def populate_main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(prog="spendguard dedup-populate")
    ap.add_argument("--input", required=True, help="the batch .jsonl that was submitted")
    ap.add_argument("--results", required=True, help="the batch results .jsonl")
    ap.add_argument("--model", default="*")
    a = ap.parse_args(argv)
    populate_jsonl(a.input, a.results, model=a.model)
    return 0


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

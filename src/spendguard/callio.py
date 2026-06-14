"""call_io — the real prompt+output corpus that makes QUALITY (cost-per-GOOD-result) computable.

The ledger says what a batch COST; it can't say whether the output was any GOOD — and a cheap call that
fails quality is 100% wasted money. So we recover the actual prompts+outputs and judge a sample.

Sources (all ZERO token cost):
  - OpenAI batches: input_file + output_file are downloadable while they exist → full prompt+output.
  - Anthropic batches: results downloadable within a 29-day window; inputs from the local request .jsonl.
  - (conversation/script mining feeds more — see conv.py.)

Storage is BOUNDED: we keep at most `cap` samples per (intent, model) — enough to estimate good% with a
confidence interval, not every request. Shares the spendguard db. Quality is written back by the caged
judge (advisor.reconstruct). RLock — reentrant.
"""
import os, json, sqlite3, threading, datetime
from . import config

_conn = None
_lock = threading.RLock()
IO_SNIP = 800            # chars kept per prompt / per output — enough to judge, bounded
DEFAULT_CAP = 50         # samples per (intent, model)


def _db():
    global _conn
    if _conn is None:
        with _lock:
            if _conn is None:
                c = sqlite3.connect(config.db_path(), timeout=10, check_same_thread=False)
                c.execute("PRAGMA journal_mode=WAL")
                c.execute("""CREATE TABLE IF NOT EXISTS call_io(
                    id TEXT PRIMARY KEY, ts TEXT, intent TEXT, provider TEXT, model TEXT,
                    batch TEXT, custom_id TEXT, prompt TEXT, output TEXT,
                    in_tok INTEGER, out_tok INTEGER,
                    quality TEXT, quality_src TEXT, quality_conf REAL, source TEXT)""")
                c.execute("CREATE INDEX IF NOT EXISTS idx_io_im ON call_io(intent, model)")
                c.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_io_key ON call_io(batch, custom_id)")
                c.commit()
                _conn = c
    return _conn


def _uid():
    import uuid
    return uuid.uuid4().hex[:16]


def count(intent, model):
    with _lock:
        return _db().execute("SELECT COUNT(*) FROM call_io WHERE intent IS ? AND model=?",
                             (intent, model)).fetchone()[0]


def counts():
    with _lock:
        return _db().execute("SELECT COALESCE(intent,'(none)'), model, COUNT(*), "
                             "SUM(quality IS NOT NULL) FROM call_io GROUP BY intent, model "
                             "ORDER BY COUNT(*) DESC").fetchall()


def record(intent, provider, model, batch, custom_id, prompt, output, in_tok=0, out_tok=0, source="batch_io"):
    """Insert one sample (idempotent on batch+custom_id). Returns id or None if duplicate."""
    cid = _uid()
    ts = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")
    try:
        with _lock:
            _db().execute(
                "INSERT OR IGNORE INTO call_io (id,ts,intent,provider,model,batch,custom_id,prompt,output,"
                "in_tok,out_tok,source) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (cid, ts, intent, provider, model, batch, str(custom_id),
                 (prompt or "")[:IO_SNIP], (output or "")[:IO_SNIP], int(in_tok or 0), int(out_tok or 0), source))
            _db().commit()
        return cid
    except Exception:
        return None


def unjudged(limit=None):
    q = "SELECT id, intent, model, prompt, output FROM call_io WHERE quality IS NULL"
    if limit:
        q += f" LIMIT {int(limit)}"
    with _lock:
        return _db().execute(q).fetchall()


def set_quality(io_id, ok, src="judge", conf=0.95):
    with _lock:
        _db().execute("UPDATE call_io SET quality=?, quality_src=?, quality_conf=? WHERE id=?",
                      ("good" if ok else "bad", src, conf, io_id))
        _db().commit()


def good_rates():
    """Per (intent, model): (n_sampled, n_judged, weighted good rate). The empirical quality signal."""
    with _lock:
        rows = _db().execute(
            "SELECT COALESCE(intent,'(none)'), model, COUNT(*), "
            "SUM(quality IS NOT NULL), "
            "SUM(CASE WHEN quality='good' THEN COALESCE(quality_conf,0.9) ELSE 0 END), "
            "SUM(CASE WHEN quality IS NOT NULL THEN COALESCE(quality_conf,0.9) ELSE 0 END) "
            "FROM call_io GROUP BY intent, model").fetchall()
    out = {}
    for intent, model, n, judged, gw, lw in rows:
        out[(intent, model)] = dict(sampled=n, judged=judged or 0,
                                    good_rate=(gw / lw) if lw else None)
    return out


# ─────────────────────────── provider retrieval (free) ───────────────────────────
def _runs_with_intent():
    from . import learn
    with learn._lock:
        rows = learn._db().execute(
            "SELECT id, label, attrs FROM graph_nodes WHERE type='run' ORDER BY ts DESC").fetchall()
    out = []
    for rid, label, attrs in rows:
        a = json.loads(attrs or "{}")
        prov = "openai" if rid.startswith("batch_") else "anthropic"
        # backfill stored model in the node LABEL as "provider:model" (not in attrs)
        model = a.get("model") or (label.split(":", 1)[1] if label and ":" in label else "?")
        out.append((rid, a.get("intent"), model, prov))
    return out


def _oai_client():
    from openai import OpenAI
    return OpenAI(api_key=config.api_key("OPENAI_API_KEY"))


def fetch_openai(client, batch_id, intent, model, cap, sample_n):
    """Sample one OpenAI batch via STREAMING (stop after sample_n lines — never download the whole
    multi-MB file). Pairs output+input by custom_id. Free."""
    b = client.batches.retrieve(batch_id)
    if not getattr(b, "output_file_id", None):
        return 0, "no output_file (expired/failed)"
    want = {}
    with client.files.with_streaming_response.content(b.output_file_id) as resp:
        for ln in resp.iter_lines():
            if not ln or not ln.strip():
                continue
            try:
                o = json.loads(ln)
            except Exception:
                continue
            cid = o.get("custom_id")
            body = (o.get("response") or {}).get("body") or {}
            msg = ((body.get("choices") or [{}])[0].get("message") or {}).get("content")
            if cid and msg is not None:
                want[cid] = {"output": msg if isinstance(msg, str) else json.dumps(msg),
                             "out_tok": (body.get("usage") or {}).get("completion_tokens", 0)}
            if len(want) >= sample_n:
                break                                   # ← early stop: don't pull the rest of the file
    # pull the matching prompts from the input file (also streamed, stop once all found)
    if getattr(b, "input_file_id", None) and want:
        need = set(want)
        with client.files.with_streaming_response.content(b.input_file_id) as resp:
            for ln in resp.iter_lines():
                if not need:
                    break
                if not ln or not ln.strip():
                    continue
                try:
                    o = json.loads(ln)
                except Exception:
                    continue
                cid = o.get("custom_id")
                if cid in need:
                    msgs = (o.get("body") or {}).get("messages") or []
                    pm = msgs[-1].get("content") if msgs else ""
                    want[cid]["prompt"] = pm if isinstance(pm, str) else json.dumps(pm)
                    need.discard(cid)
    added = 0
    for cid, d in want.items():
        if count(intent, model) >= cap:
            break
        if record(intent, "openai", model, batch_id, cid, d.get("prompt", ""), d["output"],
                  out_tok=d.get("out_tok", 0)):
            added += 1
    return added, None


def fetch_anthropic(client, batch_id, intent, model, cap, sample_n, jsonl_inputs=None):
    """Download results for one Anthropic batch (29-day window); inputs from local jsonl if provided. Free."""
    b = client.messages.batches.retrieve(batch_id)
    if not getattr(b, "results_url", None):
        return 0, "no results (expired >29d or not ended)"
    added = 0
    for res in client.messages.batches.results(batch_id):
        if count(intent, model) >= cap or added >= sample_n:
            break
        r = res.result
        if getattr(r, "type", None) != "succeeded":
            continue
        txt = "".join(blk.text for blk in r.message.content if getattr(blk, "type", None) == "text")
        out_tok = getattr(getattr(r.message, "usage", None), "output_tokens", 0)
        prompt = (jsonl_inputs or {}).get(res.custom_id, "")
        if record(intent, "anthropic", model, batch_id, res.custom_id, prompt, txt, out_tok=out_tok):
            added += 1
    return added, None


def fetch_history(cap=DEFAULT_CAP, sample_n=None, limit_batches=None):
    """Fill call_io from provider batch I/O, bounded to `cap` per (intent, model). Zero token cost.
    Skips a batch entirely once its (intent, model) quota is met — so total downloads stay bounded."""
    sample_n = sample_n or cap
    runs = _runs_with_intent()
    oc = None
    try:
        oc = _oai_client()
    except Exception:
        pass
    ac = None
    try:
        import anthropic
        ac = anthropic.Anthropic(api_key=config.api_key("ANTHROPIC_API_KEY"))
    except Exception:
        pass
    added_total, fetched, skipped_full, errors = 0, 0, 0, 0
    for i, (bid, intent, model, prov) in enumerate(runs):
        if limit_batches and fetched >= limit_batches:
            break
        if count(intent, model) >= cap:
            skipped_full += 1
            continue
        try:
            if prov == "openai" and oc:
                n, err = fetch_openai(oc, bid, intent, model, cap, sample_n)
            elif prov == "anthropic" and ac:
                n, err = fetch_anthropic(ac, bid, intent, model, cap, sample_n)
            else:
                continue
            fetched += 1
            added_total += n
            if err and n == 0:
                errors += 1
            if fetched % 5 == 0:
                print(f"  …{fetched} batches fetched, {added_total} samples so far", flush=True)
        except Exception:
            errors += 1
    return dict(added=added_total, batches_fetched=fetched, skipped_quota=skipped_full, errors=errors)


def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(prog="spendguard fetch-io")
    ap.add_argument("--cap", type=int, default=DEFAULT_CAP, help="max samples per (intent, model)")
    ap.add_argument("--limit-batches", type=int, help="cap how many batches to download (smoke test)")
    a = ap.parse_args(argv)
    print(f"fetch-io — recovering real prompt+output samples from providers (free; cap {a.cap}/intent+model)…")
    r = fetch_history(cap=a.cap, limit_batches=a.limit_batches)
    print(f"  added {r['added']} samples · {r['batches_fetched']} batches fetched · "
          f"{r['skipped_quota']} skipped (quota full) · {r['errors']} unrecoverable (expired files)")
    print(f"{'intent':<24}{'model':<22}{'sampled':>8}{'judged':>8}")
    for intent, model, n, judged in counts()[:25]:
        print(f"{intent[:23]:<24}{model[:21]:<22}{n:>8}{judged or 0:>8}")
    print("Next: `spendguard reconstruct --run` to judge these (caged Haiku) → real $/good.")
    return 0

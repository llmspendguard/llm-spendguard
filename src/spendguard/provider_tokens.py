"""provider_tokens — per-provider TEXT token estimation with a REAL tokenizer base + a MEASURED correction.

WHY THIS EXISTS. Every non-OpenAI token estimate in the repo used tiktoken-for-an-OpenAI-model or a flat
`len(s)//4` char guess. chars/4 is a crude heuristic — wrong for code (~3 chars/tok), non-latin scripts (1–2),
whitespace-heavy text — and every provider (Claude, Gemini, GLM, Kimi, Qwen, DeepSeek) tokenizes DIFFERENTLY
from GPT, so an OpenAI count is a biased proxy for them. That bias flows straight into pre-spend estimates,
bake-off/titration cost forecasts, and estimate_fan's ceiling.

THE ESTIMATOR (a pure measurement, no hand-picked constant, no trust decision):
  base   = a REAL BPE tokenization — tiktoken's o200k_base for every provider (structurally correct where
           chars/4 is not); the model's OWN exact encoding for OpenAI models, which needs no correction;
  × factor(provider) = the MEASURED o200k→native multiplier from the call_io ground-truth: the MEDIAN of
           (recorded in_tok ÷ an o200k count of the same system+prompt) over the provider's real calls. It is
           applied as measured — there is no sample-count cutoff and no "is this factor trustworthy?" judgement
           (both would be hand-picked thresholds this repo forbids). A provider with no measurement is 1.0 (the
           raw o200k proxy). The sample count `n` and the ratios' relative spread are recorded and surfaced in
           `spendguard tokens show` purely for AUDIT — so thin/noisy evidence is VISIBLE — but they gate nothing;
           the median self-sharpens as real calls accumulate (the risk window is only a provider's first calls).

There is no offline tokenizer shipped for Claude/Gemini/GLM/etc. (the vendor count-tokens endpoints are network
calls that cost latency/quota); a real-BPE base plus a measured per-provider median is the honest best available
WITHOUT a per-call network round-trip. It is EXACT for OpenAI, a measured proxy elsewhere.

FAIL-OPEN: token estimation must never break a call. tiktoken absent → a per-provider MEASURED chars/token ratio
(from the same call_io data), then a documented CHARS_PER_TOKEN_DEFAULT. Every path returns an int. A DELIBERATE
stop (spend refusal / dispatch deadline / a locked accounting period) is the one thing NOT swallowed — it
propagates, per the deliberate-refusal doctrine.
"""
import statistics
import threading

# The last-resort char→token ratio, used ONLY when tiktoken is entirely absent AND no measured ratio exists for
# the provider. Documented, not silent: ~4 chars/token is the classic English-prose rule of thumb this replaces.
CHARS_PER_TOKEN_DEFAULT = 4.0
_O200K = "o200k_base"

# IDEMPOTENT memoization of IMMUTABLE encoders (name -> encoder). A concurrent cache-miss race is SAFE: an
# encoding for a fixed name never changes at runtime, so two threads compute the SAME encoder and the last write
# stores an equal value. Only SUCCESSES are cached — a transient failure (e.g. tiktoken's first-use vocab
# download) is NOT stored, so a later call retries instead of being poisoned to a permanent None. No staleness.
_enc_cache = {}
# provider -> {"factor","char_ratio","rel_spread","n","ts"} | None (None = not yet loaded). PROCESS-LOCAL,
# published under _factor_lock as ONE atomic dict assignment (a reader sees the old or the new map, never a
# half-built one). STALENESS CONTRACT: factors change only on an explicit `calibrate`; an in-process calibrate()
# calls reload_factors() itself after committing, and a calibrate in ANOTHER process is picked up on this process's
# next start or an explicit reload_factors(). A bounded-stale CORRECTION FACTOR is acceptable for a token ESTIMATE
# (already a proxy) — it is never a spend of record, so this cache deliberately does not poll the DB per call.
_factor_cache = None
_factor_lock = threading.Lock()    # guards the _factor_cache publish/reset so a reader never sees a partial dict


def _o200k_encoder():
    """The o200k_base BPE encoder (GPT-4o/5 family), the shared REAL-tokenizer base for all providers. Cached on
    success; None if tiktoken is unavailable (then callers fall back to the measured char ratio), and NOT cached
    on failure so a transient miss can retry."""
    enc = _enc_cache.get(_O200K)
    if enc is not None:
        return enc
    try:
        import tiktoken
        enc = tiktoken.get_encoding(_O200K)
        _enc_cache[_O200K] = enc            # cache only a success (idempotent; see _enc_cache)
        return enc
    except Exception:
        return None                          # not cached → a later call retries; never a permanent None


def _encoder_for(provider, model):
    """(encoder, is_exact). is_exact=True ONLY for an OpenAI model whose OWN tiktoken encoding we resolved — that
    IS the truth, so no factor correction is applied to it. Every other provider uses the o200k PROXY (is_exact
    False) and gets the measured factor. A bare 'provider:model' is split; a missing encoder returns the proxy."""
    raw = model.split(":", 1)[1] if (model and ":" in model) else model
    if provider == "openai" and raw:
        key = f"model:{raw}"
        enc = _enc_cache.get(key)
        if enc is None:
            try:
                import tiktoken
                enc = tiktoken.encoding_for_model(raw)
                _enc_cache[key] = enc        # cache only a success (idempotent; unknown/failed id → retry, then proxy)
            except Exception:
                enc = None
        if enc is not None:
            return enc, True
    return _o200k_encoder(), False


def _factors_db():
    from . import budget
    db = budget._ledger_db()                 # same SQLite file as charges/savings/decisions
    with budget._lock:
        db.execute("CREATE TABLE IF NOT EXISTS token_factors "
                   "(provider TEXT PRIMARY KEY, factor REAL, char_ratio REAL, rel_spread REAL, n INTEGER, ts TEXT)")
        cols = [r[1] for r in db.execute("PRAGMA table_info(token_factors)")]
        if "rel_spread" not in cols:          # migrate a table created before the audit spread column existed
            db.execute("ALTER TABLE token_factors ADD COLUMN rel_spread REAL")
        db.commit()
    return db


def _stop_or_locked(e):
    """A canonical deliberate stop (gate.is_deliberate_stop — spend refusal / dispatch deadline) OR a locked-period
    write (ledger.LockedError) — the stops a fail-open reader here must PROPAGATE, never downgrade to 'keep going'.
    A superset of the canonical predicate because this module also touches the ledger, whose locked-period refusal
    is a deliberate stop too. Lazy imports keep the module low in the import graph; any hiccup → not a stop."""
    try:
        from . import gate, ledger
        return gate.is_deliberate_stop(e) or isinstance(e, ledger.LockedError)
    except Exception:
        return False


def _load_factors():
    """(Re)load the measured factors into the process cache. Fail-OPEN on a transient read failure (no table yet,
    a busy db) → empty factors (everyone uncalibrated = 1.0), never a broken estimate; but a DELIBERATE stop
    propagates. The publish is a single locked assignment of a fully-built dict, so a concurrent reader sees the
    old or the new map, never a half-built one."""
    global _factor_cache
    loaded = {}
    try:
        from . import budget
        db = _factors_db()
        with budget._lock:
            rows = db.execute("SELECT provider,factor,char_ratio,rel_spread,n,ts FROM token_factors").fetchall()
        for prov, fac, cr, rs, n, ts in rows:
            loaded[prov] = {"factor": float(fac or 1.0), "char_ratio": float(cr or CHARS_PER_TOKEN_DEFAULT),
                            "rel_spread": (float(rs) if rs is not None else None), "n": int(n or 0), "ts": ts}
    except Exception as e:
        if _stop_or_locked(e):
            raise                            # refusal / deadline / locked period → never masked by fail-open
        loaded = {}                          # transient read failure → uncalibrated (o200k proxy), never partial
    with _factor_lock:
        _factor_cache = loaded
    return loaded


def reload_factors():
    """Drop the in-process factor cache so the next estimate re-reads what calibrate() stored (called AFTER the
    calibrate commit, so a subsequent load sees the new rows). Locked so the reset can't race a concurrent publish."""
    global _factor_cache
    with _factor_lock:
        _factor_cache = None


def factor(provider):
    """(multiplier, meta) for a provider's o200k→native token correction — the MEASURED median, applied as-is.
    meta carries n + rel_spread (AUDIT ONLY — the evidence strength, surfaced so thin data is visible; they gate
    nothing) and the basis. No stored factor (or unknown provider) → (1.0, uncalibrated), the raw o200k proxy."""
    if _factor_cache is None:
        _load_factors()
    row = (_factor_cache or {}).get(provider)
    if not row:
        return 1.0, {"basis": "uncalibrated (o200k proxy)", "n": 0}
    return row["factor"], {"basis": "measured", "n": row.get("n"), "rel_spread": row.get("rel_spread"),
                           "ts": row.get("ts")}


def _char_ratio(provider):
    """Measured chars/token for a provider (the tiktoken-absent fallback), applied as-is, else CHARS_PER_TOKEN_DEFAULT."""
    if _factor_cache is None:
        _load_factors()
    row = (_factor_cache or {}).get(provider)
    if row and row.get("char_ratio"):
        return row["char_ratio"]
    return CHARS_PER_TOKEN_DEFAULT


def count_text(text, provider=None, model=None):
    """Estimated INPUT tokens for a TEXT string, provider-aware. Real BPE base × the measured provider factor;
    exact for OpenAI models. Fail-open: a tokenizer hiccup degrades to the measured char ratio (never returns 0
    for non-empty text); a DELIBERATE stop from factor-loading still propagates. This is the counter to pass as
    content_tokens' `text_tokens=`."""
    text = text or ""
    if not text:
        return 0
    try:
        enc, exact = _encoder_for(provider, model)
        if enc is not None:
            base = len(enc.encode(text))
            if exact or not provider:
                return max(1, base)
            return max(1, int(round(base * factor(provider)[0])))
    except Exception as e:
        if _stop_or_locked(e):
            raise                            # a refusal surfaced while loading factors must not be hidden as an estimate
        # any other tokenizer hiccup → the measured char-ratio fallback below
    return max(1, int(round(len(text) / _char_ratio(provider))))


def _rel_spread(vals):
    """Robust RELATIVE dispersion of the ratios (relative inter-quartile range = IQR/median) — recorded purely for
    AUDIT so a reader can see how noisy a provider's factor is. None when there are too few points (n<2) or the
    median is non-positive (undefined)."""
    if len(vals) < 2:
        return None
    med = statistics.median(vals)
    if med <= 0:
        return None
    if len(vals) < 4:                        # too few for quartiles → relative RANGE (a conservative wider spread)
        return (max(vals) - min(vals)) / med
    q1, _q2, q3 = statistics.quantiles(vals, n=4)
    return (q3 - q1) / med


def calibrate(store=True):
    """Measure each provider's o200k→native factor from the call_io ground-truth and (optionally) store it. $0 —
    reads the local replay corpus and counts with the local tokenizer; makes NO API calls.

    METHOD. For every non-truncated call_io row with a positive recorded in_tok and NO structured-output schema
    (schema framing inflates in_tok in a way the text base can't see — excluded so the ratio measures TEXT), take
    the row's own text = system+prompt, count it with o200k, and form ratio = in_tok / o200k_count. A provider's
    factor is the MEDIAN of its ratios; its rel_spread (relative IQR) and n are stored ALONGSIDE for audit. EVERY
    provider with ≥1 usable row is stored — no sample-count cutoff; the median IS the estimate. Requires tiktoken
    (the base); without it, returns an explicit note.

    Every fetched row is accounted for: `used` + Σ`skipped` == `n_rows`, so nothing is silently discarded."""
    enc = _o200k_encoder()
    if enc is None:
        return {"ok": False, "note": "tiktoken unavailable — cannot compute an o200k base to calibrate against; "
                                     "estimates use the measured char ratio only."}
    from . import callio, budget
    rows = []
    try:
        with callio._lock:
            rows = callio._callio_db().execute(
                "SELECT provider, COALESCE(system,''), COALESCE(prompt,''), in_tok "
                "FROM call_io WHERE COALESCE(truncated,0)=0 AND in_tok>0 "
                "AND (req_schema IS NULL OR req_schema='' OR req_schema='\"\"')").fetchall()
    except Exception as e:
        if _stop_or_locked(e):
            raise
        return {"ok": False, "note": f"could not read call_io ({type(e).__name__}: {str(e)[:60]})"}

    # Every fetched row is a unit; a discarded one is COUNTED by reason, never silently dropped (so the report's
    # denominator is honest: used + Σskipped == n_rows). A row is unusable for a factor when it has no provider,
    # no text to tokenize, or an empty o200k encoding — each a distinct, named rejection.
    per, skipped, used = {}, {"no_provider": 0, "no_text": 0, "empty_encoding": 0}, 0
    for prov, system, prompt, in_tok in rows:
        if not prov:
            skipped["no_provider"] += 1
            continue
        text = (system or "") + ("\n" if system and prompt else "") + (prompt or "")
        if not text:
            skipped["no_text"] += 1
            continue
        base = len(enc.encode(text))
        if base <= 0:
            skipped["empty_encoding"] += 1
            continue
        d = per.setdefault(prov, {"ratios": [], "char_ratios": []})
        d["ratios"].append(float(in_tok) / base)
        d["char_ratios"].append(float(len(text)) / float(in_tok))
        used += 1

    import datetime
    ts = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")
    report = []
    for prov, d in sorted(per.items()):
        n = len(d["ratios"])
        fac = round(statistics.median(d["ratios"]), 4)
        cr = round(statistics.median(d["char_ratios"]), 3)
        rs = _rel_spread(d["ratios"])
        report.append({"provider": prov, "factor": fac, "char_ratio": cr,
                       "rel_spread": (round(rs, 4) if rs is not None else None), "n": n})
        if store:                             # store EVERY measured provider — the median is the estimate, no cutoff
            db = _factors_db()
            with budget._lock:
                db.execute("INSERT INTO token_factors (provider,factor,char_ratio,rel_spread,n,ts) "
                           "VALUES (?,?,?,?,?,?) ON CONFLICT(provider) DO UPDATE SET factor=excluded.factor, "
                           "char_ratio=excluded.char_ratio, rel_spread=excluded.rel_spread, n=excluded.n, ts=excluded.ts",
                           (prov, fac, cr, rs, n, ts))
                db.commit()
    if store:
        reload_factors()                     # after the commit, so the next load sees the new rows
    return {"ok": True, "providers": report, "n_rows": len(rows), "used": used, "skipped": skipped,
            "note": ("stored the measured median factor for every provider (applied as-is; n + spread shown for "
                     "audit).") if store else "dry run — nothing stored."}


def cmd(argv=None):
    """`spendguard tokens [show|calibrate]` — inspect or refresh the per-provider token factors ($0)."""
    import json as _json
    argv = list(argv or [])
    do_json = "--json" in argv
    sub = next((a for a in argv if not a.startswith("-")), "show")
    if sub == "calibrate":
        dry = "--dry-run" in argv
        r = calibrate(store=not dry)
        if do_json:
            print(_json.dumps(r, indent=2)); return 0
        if not r.get("ok"):
            print(f"tokens calibrate: {r.get('note')}"); return 0
        _sk = r.get("skipped", {})
        print(f"[tokens calibrate] {r['n_rows']} call_io rows · used {r.get('used')} · "
              f"skipped {sum(_sk.values())} ({', '.join('%s=%d' % (k, v) for k, v in _sk.items() if v) or 'none'})"
              + ("  (DRY RUN — nothing stored)" if dry else ""))
        for p in r["providers"]:
            print(f"  {p['provider']:<10} factor {p['factor']:<7} char/tok {p['char_ratio']:<6} "
                  f"spread {p['rel_spread']}  n={p['n']}")
        print("  " + r["note"])
        return 0
    # show
    _load_factors()
    if do_json:
        print(_json.dumps(_factor_cache or {}, indent=2)); return 0
    if not _factor_cache:
        print("tokens: no measured factors yet — every provider uses the o200k proxy (factor 1.0). "
              "Run `spendguard tokens calibrate` after some call_io has accumulated.")
        return 0
    print("[tokens] measured o200k→native factors (median; n + spread are audit-only):")
    for prov, row in sorted(_factor_cache.items()):
        print(f"  {prov:<10} factor {row['factor']:<7} char/tok {row.get('char_ratio')} "
              f"spread {row.get('rel_spread')} n={row.get('n')} @ {row.get('ts')}")
    return 0

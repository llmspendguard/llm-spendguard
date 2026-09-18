"""provider_tokens — per-provider TEXT token estimation with a REAL tokenizer base + an AGENTICALLY-chosen factor.

WHY THIS EXISTS. Every non-OpenAI token estimate in the repo used tiktoken-for-an-OpenAI-model or a flat
`len(s)//4` char guess. chars/4 is a crude heuristic — wrong for code (~3 chars/tok), non-latin scripts (1–2),
whitespace-heavy text — and every provider (Claude, Gemini, GLM, Kimi, Qwen, DeepSeek) tokenizes DIFFERENTLY
from GPT, so an OpenAI count is a biased proxy for them. That bias flows straight into pre-spend estimates,
bake-off/titration cost forecasts, and estimate_fan's ceiling.

THE ESTIMATOR:
  base   = a REAL BPE tokenization — tiktoken's o200k_base for every provider (structurally correct where
           chars/4 is not); the model's OWN exact encoding for OpenAI models, which needs no correction;
  × factor(provider) = an o200k→native multiplier CHOSEN AGENTICALLY from the measured statistics. calibrate()
           computes, per provider from the call_io ground-truth, the full panel — n, the MEDIAN per-call ratio,
           the token-weighted AGGREGATE (Σin_tok/Σo200k, the least-biased estimator for TOTAL cost), the mean,
           the p10/p90, and the relative spread — then hands the whole panel to ONE gated meta LLM call that
           picks each provider's factor with a confidence and a one-line rationale. "Which central estimate, and
           how far to trust it at this n and spread" is a JUDGEMENT (two reasonable people would weigh median vs
           aggregate differently), so it is decided by the model, not a hand-picked cutoff or shrinkage constant.
           It is decided ONCE per `spendguard tokens calibrate` (user-invoked, periodic) — a tiny cost — and the
           per-call count_text path stays $0 (it applies the stored number). The model's rationale is stored, so
           every factor is auditable. A provider with no chosen factor is 1.0 (the raw o200k proxy).

There is no offline tokenizer shipped for Claude/Gemini/GLM/etc. (the vendor count-tokens endpoints are network
calls that cost latency/quota); a real-BPE base plus an agentically-chosen measured correction is the honest best
available WITHOUT a per-call network round-trip. It is EXACT for OpenAI, a judged proxy elsewhere.

FAIL-OPEN: token estimation must never break a call. tiktoken absent → a per-provider MEASURED chars/token ratio
(median, the last-resort fallback), then CHARS_PER_TOKEN_DEFAULT. Every path returns an int. A DELIBERATE stop
(spend refusal / dispatch deadline / a locked accounting period) is the one thing NOT swallowed — it propagates.
"""
import json
import statistics
import threading

# The last-resort char→token ratio, used ONLY when tiktoken is entirely absent AND no measured ratio exists for
# the provider. Documented, not silent: ~4 chars/token is the classic English-prose rule of thumb this replaces.
CHARS_PER_TOKEN_DEFAULT = 4.0
_O200K = "o200k_base"
_FACTOR_OUT = 900          # output cap for the one factor-choice meta call (a small JSON list, few providers)

# IDEMPOTENT memoization of IMMUTABLE encoders (name -> encoder). A concurrent cache-miss race is SAFE: an
# encoding for a fixed name never changes at runtime, so two threads compute the SAME encoder and the last write
# stores an equal value. Only SUCCESSES are cached — a transient failure (e.g. tiktoken's first-use vocab
# download) is NOT stored, so a later call retries instead of being poisoned to a permanent None. No staleness.
_enc_cache = {}
# provider -> {"factor","confidence","why","char_ratio","n","stats","ts"} | None (None = not yet loaded).
# PROCESS-LOCAL, published under _factor_lock as ONE atomic dict assignment (a reader sees the old or the new map,
# never a half-built one). STALENESS CONTRACT: factors change only on an explicit `calibrate`; an in-process
# calibrate() calls reload_factors() itself after committing, and a calibrate in ANOTHER process is picked up on
# this process's next start or an explicit reload_factors(). A bounded-stale CORRECTION FACTOR is acceptable for a
# token ESTIMATE (already a proxy) — never a spend of record — so this cache deliberately does not poll per call.
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
    False) and gets the chosen factor. A bare 'provider:model' is split; a missing encoder returns the proxy."""
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
                   "(provider TEXT PRIMARY KEY, factor REAL, confidence REAL, why TEXT, char_ratio REAL, "
                   "n INTEGER, stats TEXT, ts TEXT)")
        cols = [r[1] for r in db.execute("PRAGMA table_info(token_factors)")]
        for c, decl in (("confidence", "REAL"), ("why", "TEXT"), ("char_ratio", "REAL"),
                        ("n", "INTEGER"), ("stats", "TEXT")):    # migrate a table created before these columns
            if c not in cols:
                db.execute(f"ALTER TABLE token_factors ADD COLUMN {c} {decl}")
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
    """(Re)load the chosen factors into the process cache. Fail-OPEN on a transient read failure (no table yet, a
    busy db) → empty factors (everyone uncalibrated = 1.0), never a broken estimate; but a DELIBERATE stop
    propagates. The publish is a single locked assignment of a fully-built dict, so a concurrent reader sees the
    old or the new map, never a half-built one."""
    global _factor_cache
    loaded = {}
    try:
        from . import budget
        db = _factors_db()
        with budget._lock:
            rows = db.execute("SELECT provider,factor,confidence,why,char_ratio,n,stats,ts FROM token_factors").fetchall()
        for prov, fac, conf, why, cr, n, stats, ts in rows:
            loaded[prov] = {"factor": float(fac or 1.0), "confidence": (float(conf) if conf is not None else None),
                            "why": why, "char_ratio": float(cr or CHARS_PER_TOKEN_DEFAULT),
                            "n": int(n or 0), "stats": stats, "ts": ts}
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
    """(multiplier, meta) for a provider's o200k→native token correction — the AGENTICALLY-chosen factor, applied
    as-is (the model already weighed which central estimate and how far to trust it). meta carries the model's
    confidence + rationale + n for audit. No stored factor (or unknown provider) → (1.0, uncalibrated proxy)."""
    if _factor_cache is None:
        _load_factors()
    row = (_factor_cache or {}).get(provider)
    if not row:
        return 1.0, {"basis": "uncalibrated (o200k proxy)", "n": 0}
    return row["factor"], {"basis": "agentic", "n": row.get("n"), "confidence": row.get("confidence"),
                           "why": row.get("why"), "ts": row.get("ts")}


def _char_ratio(provider):
    """Measured chars/token for a provider (the tiktoken-absent fallback), applied as-is, else CHARS_PER_TOKEN_DEFAULT.
    A last-resort measurement (fires only when the whole o200k base is unavailable), not the primary judged path."""
    if _factor_cache is None:
        _load_factors()
    row = (_factor_cache or {}).get(provider)
    if row and row.get("char_ratio"):
        return row["char_ratio"]
    return CHARS_PER_TOKEN_DEFAULT


def count_text(text, provider=None, model=None):
    """Estimated INPUT tokens for a TEXT string, provider-aware. Real BPE base × the chosen provider factor; exact
    for OpenAI models. Fail-open: a tokenizer hiccup degrades to the measured char ratio (never returns 0 for
    non-empty text); a DELIBERATE stop from factor-loading still propagates. This is the counter to pass as
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


def _provider_stats(ratios, char_ratios, in_sum, o_sum):
    """The full statistic panel the model chooses a factor FROM — median / token-weighted aggregate / mean / p10 /
    p90 / relative-IQR spread / n. The aggregate (Σin_tok÷Σo200k) is the least-biased central estimate for TOTAL
    cost; the others describe the distribution's shape so the model can judge which to trust and how far."""
    n = len(ratios)
    med = round(statistics.median(ratios), 4)
    agg = round(in_sum / o_sum, 4) if o_sum > 0 else None
    mean = round(statistics.fmean(ratios), 4)
    _s = sorted(ratios)                                        # nearest-rank percentile, inline (no module-level helper)
    _q = lambda qq: _s[min(len(_s) - 1, max(0, int(round(qq * (len(_s) - 1)))))] if _s else None
    p10, p90 = _q(0.10), _q(0.90)
    spread = None
    if n >= 2 and med > 0:
        if n >= 4:
            q1, _q2, q3 = statistics.quantiles(ratios, n=4)
            spread = round((q3 - q1) / med, 4)
        else:
            spread = round((max(ratios) - min(ratios)) / med, 4)
    return {"n": n, "median": med, "aggregate": agg, "mean": mean,
            "p10": (round(p10, 4) if p10 is not None else None),
            "p90": (round(p90, 4) if p90 is not None else None), "rel_spread": spread,
            "char_ratio_median": round(statistics.median(char_ratios), 3)}


_FACTOR_SYS = (
    "You calibrate per-provider TOKEN-COUNT correction factors used for COST estimation. Base counts come from "
    "OpenAI's o200k tokenizer; a provider's TRUE input-token count is estimated as o200k_count × factor. You are "
    "given measured statistics per provider from real calls, where each call's ratio = provider_reported_in_tok / "
    "o200k_count(system+prompt). Choose ONE factor per provider. Guidance: the token-weighted AGGREGATE "
    "(sum_in_tok / sum_o200k) is the least-biased central estimate for total cost; the MEDIAN is robust to "
    "outliers; weigh them using n and the spread. When evidence is THIN (small n) or NOISY (wide rel_spread / "
    "p10-p90 gap), pull the factor TOWARD 1.0 (the neutral o200k proxy) — an overconfident correction from little "
    "data is worse than none. A factor must be > 0. Return, per provider, ONLY the providers you were given: "
    "factor, confidence in [0,1], and a one-line why naming which statistic you leaned on and why.")

_FACTOR_SCHEMA = {
    "type": "object",
    "properties": {
        "providers": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "provider": {"type": "string"},
                    "factor": {"type": "number"},
                    "confidence": {"type": "number"},
                    "why": {"type": "string"},
                },
                "required": ["provider", "factor", "confidence", "why"],
            },
        }
    },
    "required": ["providers"],
}


def _choose_factors_agentically(stats_by_provider, model):
    """ONE gated meta call: hand the model the full per-provider statistic panel and let it choose each factor +
    confidence + rationale. Returns ({provider: {factor, confidence, why}}, cost). A DELIBERATE stop propagates; any
    other failure returns (None, cost) so the caller can leave those providers on the neutral proxy (never a
    mechanical substitute dressed as the judged choice). The WHOLE panel is sent (never truncated)."""
    from . import adapters, calls, config
    model = model or config.advisor_model()
    prompt = "Per-provider token-ratio statistics (ratio = provider_in_tok / o200k_count):\n" + json.dumps(
        stats_by_provider, indent=2, sort_keys=True) + "\n\nChoose a factor for each provider listed."
    try:
        with calls.context(intent="spendguard:token-calibrate"):
            r = adapters.call(model, prompt, max_tokens=_FACTOR_OUT, system=_FACTOR_SYS,
                              schema=_FACTOR_SCHEMA, sig="spendguard:token-calibrate")
    except Exception as e:
        if _stop_or_locked(e):
            raise
        return None, 0.0
    cost = r.get("cost") or 0.0
    if r.get("error"):
        return None, cost
    j = adapters.structured_reply(r)
    if not isinstance(j, dict) or not isinstance(j.get("providers"), list):
        return None, cost
    out = {}
    for it in j["providers"]:
        if isinstance(it, dict) and it.get("provider") and isinstance(it.get("factor"), (int, float)) and it["factor"] > 0:
            out[str(it["provider"])] = {"factor": float(it["factor"]),
                                        "confidence": float(it.get("confidence") or 0.0),
                                        "why": str(it.get("why") or "")}
    return (out or None), cost


def _estimate_choice_cost(stats_by_provider, model=None):
    """$0 estimate of the ONE factor-choice meta call, for --dry-run (the estimate-first discipline). A DELIBERATE
    stop from pricing (EstimateNotGrounded / a refusal) PROPAGATES — it is not masked into a None estimate."""
    from . import config, pricing, adapters
    model = model or config.advisor_model()
    prompt = _FACTOR_SYS + "\n" + json.dumps(stats_by_provider, sort_keys=True)
    in_tok = count_text(prompt, provider=adapters.provider_for(model), model=model)
    try:
        return round(float(pricing.realtime_cost(model, in_tok, _FACTOR_OUT) or 0.0), 6)
    except Exception as e:
        if _stop_or_locked(e):
            raise                            # EstimateNotGrounded / a refusal must PROPAGATE, never masked as None
        return None


def calibrate(store=True, model=None):
    """Measure each provider's ratio statistics from the call_io ground-truth, then AGENTICALLY choose each factor.
    Reading + counting is $0; the factor CHOICE is one small gated meta call (like `recommend`/`bakeoff`, the user
    invoking this is the approval). `store=False` (--dry-run) computes + returns the stats AND a $0 estimate of the
    choice call, and makes NO call.

    METHOD. For every non-truncated call_io row with a positive recorded in_tok and NO structured-output schema
    (schema framing inflates in_tok in a way the text base can't see — excluded so the ratio measures TEXT), take
    the row's own text = system+prompt, count it with o200k, and accumulate ratio = in_tok/o200k plus Σin_tok and
    Σo200k. The full panel (n, median, aggregate, mean, p10, p90, spread) goes to _choose_factors_agentically,
    whose per-provider factor + confidence + rationale are stored. char_ratio (the tiktoken-absent fallback) is the
    measured median. If the meta choice is unavailable, providers are LEFT on the neutral proxy (1.0) with a note —
    never a mechanical factor dressed as the judged one. Every fetched row is accounted for (used + Σskipped ==
    n_rows); every returned choice is accounted for (stored + Σignored == choices). Requires tiktoken (the base)."""
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
    # denominator is honest: used + Σskipped == n_rows). A row is unusable when it has no provider, no text to
    # tokenize, or an empty o200k encoding — each a distinct, named rejection.
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
        d = per.setdefault(prov, {"ratios": [], "char_ratios": [], "in_sum": 0, "o_sum": 0})
        d["ratios"].append(float(in_tok) / base)
        d["char_ratios"].append(float(len(text)) / float(in_tok))
        d["in_sum"] += float(in_tok)
        d["o_sum"] += float(base)
        used += 1

    stats_by_provider = {prov: _provider_stats(d["ratios"], d["char_ratios"], d["in_sum"], d["o_sum"])
                         for prov, d in sorted(per.items())}
    base_report = {"ok": True, "n_rows": len(rows), "used": used, "skipped": skipped, "stats": stats_by_provider}
    if not store:
        return {**base_report, "est_choice_cost": _estimate_choice_cost(stats_by_provider, model),
                "note": "dry run — statistics + a $0 estimate of the choice call; the factor CHOICE is skipped."}
    if not stats_by_provider:
        return {**base_report, "chosen": {}, "note": "no usable call_io rows yet — nothing to calibrate ($0)."}

    choices, cost = _choose_factors_agentically(stats_by_provider, model)   # raises on a deliberate stop
    if not choices:
        return {**base_report, "chosen": {}, "meta_cost": round(cost, 6),
                "note": "the agentic factor choice was unavailable (no meta budget / a transient failure) — every "
                        "provider stays on the neutral o200k proxy (1.0). Re-run when the meta path is available."}

    import datetime
    ts = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")
    # Every RETURNED choice is accounted for: a choice for a provider we did NOT measure (a model hallucination /
    # stale name) has no statistical basis and is REFUSED — but recorded in `ignored`, never silently dropped, so
    # stored + Σignored == len(choices).
    stored, ignored = {}, []
    for prov, ch in choices.items():
        st = stats_by_provider.get(prov)
        if st is None:
            ignored.append(prov)
            continue
        db = _factors_db()
        with budget._lock:
            db.execute("INSERT INTO token_factors (provider,factor,confidence,why,char_ratio,n,stats,ts) "
                       "VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(provider) DO UPDATE SET factor=excluded.factor, "
                       "confidence=excluded.confidence, why=excluded.why, char_ratio=excluded.char_ratio, "
                       "n=excluded.n, stats=excluded.stats, ts=excluded.ts",
                       (prov, ch["factor"], ch["confidence"], ch["why"], st["char_ratio_median"], st["n"],
                        json.dumps(st), ts))
            db.commit()
        stored[prov] = {**ch, "n": st["n"]}
    reload_factors()                          # after the commit, so the next load sees the new rows
    note = ("factors CHOSEN agentically from the statistics (one meta call) and stored; count_text applies them at "
            "$0. n + rationale are surfaced in `tokens show` for audit.")
    if ignored:
        note += " Ignored %d model-named provider(s) with no measured basis: %s." % (len(ignored), ", ".join(ignored))
    return {**base_report, "chosen": stored, "ignored": ignored, "meta_cost": round(cost, 6), "note": note}


def cmd(argv=None):
    """`spendguard tokens [show|calibrate]` — inspect or refresh the per-provider token factors."""
    import json as _json
    argv = list(argv or [])
    do_json = "--json" in argv
    sub = next((a for a in argv if not a.startswith("-")), "show")
    if sub == "calibrate":
        dry = "--dry-run" in argv
        r = calibrate(store=not dry, model=None)
        if do_json:
            print(_json.dumps(r, indent=2)); return 0
        if not r.get("ok"):
            print(f"tokens calibrate: {r.get('note')}"); return 0
        _sk = r.get("skipped", {})
        tail = (f"  (DRY RUN — statistics only; est choice call ${r.get('est_choice_cost')})" if dry
                else f"  · meta cost ${r.get('meta_cost', 0)}")
        print(f"[tokens calibrate] {r['n_rows']} call_io rows · used {r.get('used')} · "
              f"skipped {sum(_sk.values())} ({', '.join('%s=%d' % (k, v) for k, v in _sk.items() if v) or 'none'})" + tail)
        for prov, st in sorted((r.get("stats") or {}).items()):
            ch = (r.get("chosen") or {}).get(prov)
            picked = (f"→ factor {ch['factor']} (conf {ch['confidence']})" if ch else "→ (proxy 1.0)")
            print(f"  {prov:<10} n={st['n']:<4} median {st['median']} agg {st['aggregate']} "
                  f"spread {st['rel_spread']}  {picked}")
            if ch and ch.get("why"):
                print(f"             why: {ch['why']}")
        if r.get("ignored"):
            print(f"  ignored (no measured basis): {', '.join(r['ignored'])}")
        print("  " + r["note"])
        return 0
    # show
    _load_factors()
    if do_json:
        print(_json.dumps(_factor_cache or {}, indent=2)); return 0
    if not _factor_cache:
        print("tokens: no chosen factors yet — every provider uses the o200k proxy (factor 1.0). "
              "Run `spendguard tokens calibrate` after some call_io has accumulated.")
        return 0
    print("[tokens] agentically-chosen o200k→native factors (with the model's rationale, for audit):")
    for prov, row in sorted(_factor_cache.items()):
        print(f"  {prov:<10} factor {row['factor']:<7} conf {row.get('confidence')} n={row.get('n')} @ {row.get('ts')}")
        if row.get("why"):
            print(f"             why: {row['why']}")
    return 0

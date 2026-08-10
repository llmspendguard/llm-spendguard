"""CANONICAL OpenAI pricing — the single source of truth for $ estimates.

Prices are USD per 1,000,000 tokens.
VERIFIED against https://developers.openai.com/api/docs/pricing on 2026-06-13.

WHY THIS FILE EXISTS
--------------------
GPT-5.5 was hardcoded as (1.25, 10.0) in ~10 scripts (loinc_*, llm_gold_v16,
batch_submit_guard, ...). The real batch rate is (2.50, 15.00) — input 2x and
output 1.5x higher than those literals, i.e. realtime is 4x/3x higher. Every
"est ~$X" produced by those scripts was ~3-4x too low, which is why cost-conscious
days still produced $200+/day charges. NEVER hardcode a price again — import here.

USAGE
-----
    import sys, os; sys.path.insert(0, os.path.join(os.path.dirname(__file__)))
    from pricing import batch_cost, realtime_cost, estimate, price

    c = batch_cost("gpt-5.5", in_tok=359_724, out_tok=21_705)   # -> dollars
    e = estimate("gpt-5.5", n=24_000, avg_in=340, avg_out=600, batch=True)

Run `python scripts/pricing.py` to print the table and self-check.
"""
import re, os, json, datetime

# per 1M tokens: realtime in/out, cached input, batch in/out  (batch == 50% of realtime).
# This dict is the FALLBACK / source-of-record for the shipped prices.json. At runtime
# prices load from config (prices.json + optional user override); if that fails, this is used.
_FALLBACK = {
    # ---- current flagship (on the live pricing page, verified 2026-06-13) ----
    "gpt-5.5":      dict(provider="openai", in_=5.00,  out=30.00,  cached_in=0.50,  batch_in=2.50,  batch_out=15.00),
    "gpt-5.5-pro":  dict(provider="openai", in_=30.00, out=180.00, cached_in=3.00,  batch_in=15.00, batch_out=90.00),
    "gpt-5.4":      dict(provider="openai", in_=2.50,  out=15.00,  cached_in=0.25,  batch_in=1.25,  batch_out=7.50),
    "gpt-5.4-mini": dict(provider="openai", in_=0.75,  out=4.50,   cached_in=0.075, batch_in=0.375, batch_out=2.25),
    "gpt-5.4-nano": dict(provider="openai", in_=0.20,  out=1.25,   cached_in=0.02,  batch_in=0.10,  batch_out=0.625),
    # ---- legacy (not on current page; stable historical rates) ----
    "gpt-5":        dict(provider="openai", in_=1.25,  out=10.00,  cached_in=0.125, batch_in=0.625, batch_out=5.00),
    "gpt-5-mini":   dict(provider="openai", in_=0.25,  out=2.00,   cached_in=0.025, batch_in=0.125, batch_out=1.00),
    "gpt-5-nano":   dict(provider="openai", in_=0.05,  out=0.40,   cached_in=0.005, batch_in=0.025, batch_out=0.20),
    "gpt-4o":       dict(provider="openai", in_=2.50,  out=10.00,  cached_in=1.25,  batch_in=1.25,  batch_out=5.00),
    "gpt-4o-mini":  dict(provider="openai", in_=0.15,  out=0.60,   cached_in=0.075, batch_in=0.075, batch_out=0.30),
    "gpt-4.1-mini": dict(provider="openai", in_=0.40,  out=1.60,   cached_in=0.10,  batch_in=0.20,  batch_out=0.80),
    "text-embedding-3-large": dict(provider="openai", in_=0.13, out=0.0, cached_in=0.0, batch_in=0.065, batch_out=0.0),
    "text-embedding-3-small": dict(provider="openai", in_=0.02, out=0.0, cached_in=0.0, batch_in=0.010, batch_out=0.0),
    # ---- Anthropic Claude (verified via claude-api skill, 2026-06-13). batch = 50% off;
    #      cached_in = cache-READ (~0.1x in). Cache WRITE is ~1.25x in @5min / 2x @1h (not stored here).
    #      NOTE: claude-opus-4-8 is $5/$25 — NOT the old $15/$75 (that was Opus 3/4/4.1). Opus 4.8 < gpt-5.5 on output.
    "claude-opus-4-8":   dict(provider="anthropic", in_=5.00, out=25.00, cached_in=0.50, batch_in=2.50, batch_out=12.50),
    "claude-sonnet-4-6": dict(provider="anthropic", in_=3.00, out=15.00, cached_in=0.30, batch_in=1.50, batch_out=7.50),
    "claude-haiku-4-5":  dict(provider="anthropic", in_=1.00, out=5.00,  cached_in=0.10, batch_in=0.50, batch_out=2.50),
    # legacy Claude (stable historical rates) — for reconciling older batches
    "claude-opus-4-7":   dict(provider="anthropic", in_=5.00,  out=25.00, cached_in=0.50, batch_in=2.50,  batch_out=12.50),
    "claude-opus-4-6":   dict(provider="anthropic", in_=5.00,  out=25.00, cached_in=0.50, batch_in=2.50,  batch_out=12.50),
    "claude-opus-4-5":   dict(provider="anthropic", in_=5.00,  out=25.00, cached_in=0.50, batch_in=2.50,  batch_out=12.50),
    "claude-opus-4-1":   dict(provider="anthropic", in_=15.00, out=75.00, cached_in=1.50, batch_in=7.50,  batch_out=37.50),
    "claude-opus-4-0":   dict(provider="anthropic", in_=15.00, out=75.00, cached_in=1.50, batch_in=7.50,  batch_out=37.50),
    "claude-sonnet-4-5": dict(provider="anthropic", in_=3.00,  out=15.00, cached_in=0.30, batch_in=1.50,  batch_out=7.50),
    "claude-sonnet-4-0": dict(provider="anthropic", in_=3.00,  out=15.00, cached_in=0.30, batch_in=1.50,  batch_out=7.50),
}

PRICING_SOURCE = "https://developers.openai.com/api/docs/pricing"
PRICING_VERIFIED = "2026-06-13"
STALE_AFTER_DAYS = 45
PROVIDERS = {}  # model -> provider, populated by _load (absent when two vendors disagree — see below)

# Bare model ids that MORE THAN ONE vendor publishes at DIFFERENT rates. Such an id has no bare answer, so
# it gets no bare entry and lookups must name a provider. Recording the ambiguity is the point: an empty
# result would be indistinguishable from "no such model", and a silently-picked one is a wrong invoice.
AMBIGUOUS_BARE = set()


def _rate_key(rates):
    """The billing identity of a rate card. Two entries are the same money iff these four numbers match.

    Exact comparison of published $/token from a fixed schema — arithmetic, not a judgement about meaning.
    It lives here because BOTH the loader (deciding whether a bare id is ambiguous) and _vendor_qualified
    (deciding whether candidate vendors disagree) ask the identical question, and asked it in two places
    with two inline tuples. One of them could drift from the other and nothing would notice: the loader
    would collapse a collision the resolver considered ambiguous, and the resolver's guard would never
    fire."""
    return (rates.get("in_"), rates.get("out"), rates.get("batch_in"), rates.get("batch_out"))


def user_prices_path():
    """Where a user's OWN verified prices live (highest precedence after $SPENDGUARD_PRICES)."""
    home = os.environ.get("SPENDGUARD_HOME") or os.path.expanduser("~/.spendguard")
    return os.path.join(home, "prices.json")


def set_price(model, provider, in_usd, out_usd, source, batch_in=None, batch_out=None, cached_in=None):
    """Record a VERIFIED price for a model spendguard cannot price. Rates are $ per 1M tokens.

    `source` is REQUIRED and stored with the entry. That is the whole discipline: spendguard never invents a
    price (a fabricated glm-5.2 stub once under-priced a model ~40%), so the only way one enters the table is a
    human writing down where the number came from. Non-positive rates are refused — a $0 price is what makes
    real spend record as free, silently."""
    if not model or not provider:
        raise ValueError("model and provider are both required")
    if not source or not str(source).strip():
        raise ValueError("a --source is required: prices enter this table only with provenance, never invented")
    try:
        in_usd, out_usd = float(in_usd), float(out_usd)
    except (TypeError, ValueError):
        raise ValueError("--in and --out must be numbers ($ per 1M tokens)")
    if in_usd <= 0 or out_usd <= 0:
        raise ValueError("rates must be positive — a $0 rate records real spend as free (see sync's zero-rate skip)")
    path = user_prices_path()
    # AN UNREADABLE FILE IS NOT AN EMPTY ONE. `except: data = {}` followed by a full rewrite meant a single
    # partial write, permission blip or stray character DESTROYED every verified price the user had
    # recorded — in the file whose entire discipline is "prices enter this table only with provenance,
    # never invented". Losing them is worse than refusing to add one, and the refusal is recoverable.
    data = {}
    if os.path.exists(path):
        try:
            with open(path) as _fh:
                data = json.load(_fh)
        except Exception as e:
            raise ValueError(
                f"{path} exists but could not be read ({type(e).__name__}: {str(e)[:80]}). REFUSING to "
                f"write, because doing so would replace every price recorded in it with just this one. "
                f"Fix or move that file, then re-run.")
        if not isinstance(data, dict):
            raise ValueError(f"{path} does not contain a JSON object — refusing to overwrite it.")
    provs = data.setdefault("providers", {}).setdefault(str(provider).strip().lower(), {})
    entry = {"in_": in_usd, "out": out_usd,
             "cached_in": float(cached_in) if cached_in is not None else round(in_usd * 0.1, 6),
             "batch_in": float(batch_in) if batch_in is not None else round(in_usd * 0.5, 6),
             "batch_out": float(batch_out) if batch_out is not None else round(out_usd * 0.5, 6),
             "_source": str(source).strip(),
             "_added": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")}
    provs.setdefault("models", {})[str(model).strip()] = entry
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as fh:
        json.dump(data, fh, indent=2, sort_keys=True)
    return path, entry


def _candidate_files():
    """Config files, lowest→highest precedence. Later overrides earlier (user can override one model)."""
    here = os.path.dirname(os.path.abspath(__file__))
    paths = [os.path.join(here, "prices.json")]                         # shipped default
    home = os.environ.get("SPENDGUARD_HOME") or os.path.expanduser("~/.spendguard")
    paths += [os.path.join(home, "prices.json"), os.path.join(home, "prices.yaml")]  # user override
    if os.environ.get("SPENDGUARD_PRICES"):
        paths.append(os.environ["SPENDGUARD_PRICES"])                   # explicit override
    return [p for p in paths if os.path.exists(p)]


def _read(path):
    txt = open(path).read()
    if path.endswith((".yaml", ".yml")):
        import yaml  # optional; only needed if a YAML config is used
        return yaml.safe_load(txt)
    return json.loads(txt)


def _load():
    """Build PRICING (model->rates) + PROVIDERS (model->provider) by layering, lowest→highest precedence:
       built-in _FALLBACK  →  LiteLLM cache (breadth, from `spendguard sync-prices`)  →
       curated prices.json (our verified models win)  →  user override. No network here (cache only)."""
    global PRICING_SOURCE, PRICING_VERIFIED, STALE_AFTER_DAYS, PROVIDERS
    prices = dict(_FALLBACK)
    # THE DATA CARRIES ITS OWN PROVIDER. This line used to read
    #     {m: ("anthropic" if m.startswith("claude") else "openai") for m in _FALLBACK}
    # which was correct for all 23 entries and a trap for the 24th: the first Moonshot or z.ai fallback
    # added would have been attributed to OpenAI — silently, in the table the entire ledger prices from,
    # with an `else` branch that can only ever name one vendor. An inference that happens to be right is
    # still an inference. Refuse a rate with no vendor rather than invent one; the same discipline
    # set_price() already applies to `source`.
    PROVIDERS = {}
    for _m, _rates in _FALLBACK.items():
        _prov = _rates.get("provider")
        if not _prov:
            raise ValueError(f"_FALLBACK[{_m!r}] has no provider — a rate that cannot be attributed to a "
                             "vendor cannot be billed to one either")
        PROVIDERS[_m] = _prov
    # LiteLLM breadth (2700+ models) — cached locally; absent until `spendguard sync-prices` runs.
    home = os.environ.get("SPENDGUARD_HOME") or os.path.expanduser("~/.spendguard")
    litellm = os.path.join(home, "litellm_prices.json")
    if os.path.exists(litellm):
        try:
            d = json.load(open(litellm))
            prices.update(d.get("models", {}))
            PROVIDERS.update(d.get("providers", {}))
        except Exception as e:
            import sys
            sys.stderr.write(f"[pricing] WARN could not load LiteLLM cache ({e})\n")
    for path in _candidate_files():
        try:
            cfg = _read(path)
            meta = cfg.get("_meta", {})
            PRICING_SOURCE = meta.get("source", PRICING_SOURCE)
            PRICING_VERIFIED = meta.get("verified", PRICING_VERIFIED)
            STALE_AFTER_DAYS = meta.get("stale_after_days", STALE_AFTER_DAYS)
            for prov, pd in (cfg.get("providers") or {}).items():
                for model, rates in (pd.get("models") or {}).items():
                    # KEEP THE PROVIDER. The config knows exactly which vendor publishes this rate, and
                    # flattening to a bare key threw that away: two vendors hosting the same model id meant
                    # the LAST one loaded silently overwrote both the rate and the attribution of the first.
                    # Dict iteration order decided which vendor's money was used.
                    #
                    # _vendor_qualified() below already resolves `vendor/model` and already RAISES when two
                    # vendors publish DIFFERENT rates for one bare id rather than picking. That guard could
                    # never fire, because the collision was resolved here — before anything could see it.
                    prices[f"{prov}/{model}"] = rates
                    prior = prices.get(model)
                    if prior is not None and _rate_key(prior) != _rate_key(rates):
                        # Same id, two vendors, DIFFERENT money. Leave NO bare entry: a caller who did not
                        # name a provider must reach the ambiguity error, not one of the two answers.
                        prices.pop(model, None)
                        AMBIGUOUS_BARE.add(model)
                        PROVIDERS.pop(model, None)
                    elif model not in AMBIGUOUS_BARE:
                        prices[model] = rates
                        PROVIDERS[model] = prov
        except Exception as e:
            import sys
            sys.stderr.write(f"[pricing] WARN could not load {path} ({e}); using built-in fallback\n")
    return prices


PRICING = _load()


def _load_units():
    """UNIT_PRICES {kind: {model[:variant]: usd}} for non-token billing — kinds: `image` (per image,
    variant 'WxH:quality'), `audio_second` (transcription $/second), `tts_char` ($/character),
    `training_token` (fine-tune training $/token). Layered like PRICING, lowest→highest:
    LiteLLM cache per-unit fields (input_cost_per_second / *_per_character / *_per_image — provenance:
    the synced upstream table)  →  curated prices.json `unit_prices`. NOTHING is guessed: an absent
    unit price surfaces as a loud unpriced-call WARN at the gate, never a made-up number."""
    units = {"image": {}, "audio_second": {}, "tts_char": {}, "training_token": {}, "web_search_call": {}}
    home = os.environ.get("SPENDGUARD_HOME") or os.path.expanduser("~/.spendguard")
    lit = os.path.join(home, "litellm_prices.json")
    if os.path.exists(lit):
        try:
            for model, r in (json.load(open(lit)).get("unit_models") or {}).items():
                if r.get("input_cost_per_second"):
                    units["audio_second"][model] = float(r["input_cost_per_second"])
                per_char = r.get("input_cost_per_character") or r.get("output_cost_per_character")
                if per_char:
                    units["tts_char"][model] = float(per_char)
                per_img = r.get("output_cost_per_image") or r.get("input_cost_per_image")
                if per_img:
                    units["image"][model] = float(per_img)
        except Exception as e:
            import sys
            sys.stderr.write(f"[pricing] WARN could not load LiteLLM unit prices ({e})\n")
    for path in _candidate_files():
        try:
            for kind, entries in (_read(path).get("unit_prices") or {}).items():
                if kind in units:
                    units[kind].update({k: float(v) for k, v in entries.items()})
        except Exception:
            pass
    return units


UNIT_PRICES = _load_units()


def _load_context():
    """{model: {max_input_tokens, max_output_tokens}} from the synced LiteLLM cache. These are LIMITS, not
    prices — the upstream table is `model_prices_and_context_window.json` and carries both. Absent → {}, and
    every caller must treat "unknown" as "no opinion" rather than inventing a bound."""
    home = os.environ.get("SPENDGUARD_HOME") or os.path.expanduser("~/.spendguard")
    try:
        return json.load(open(os.path.join(home, "litellm_prices.json"))).get("context") or {}
    except Exception:
        return {}


CONTEXT_LIMITS = _load_context()


def max_output_tokens(model: str):
    """The model's published output ceiling, or None. Used as the LAST-RESORT expected-output figure when a
    caller sets no max_tokens and the class has no measured history — and, for providers that REQUIRE the
    field (Anthropic), as the honest 'deliberately huge' value instead of an invented constant."""
    if not model:
        return None
    for key in (model, normalize(model)):
        e = CONTEXT_LIMITS.get(key) or {}
        v, ctx = e.get("max_output_tokens"), e.get("max_input_tokens")
        if not v:
            continue
        # 961 of 2,572 upstream entries carry output == input: that is the CONTEXT WINDOW copied in where no
        # output ceiling is published, not an output limit. Returning it would assume a 1M-token response and
        # inflate every estimate — a field meaning one thing read as another, the same root as base64-as-tokens.
        # Equal values are therefore treated as UNPUBLISHED, not as a limit.
        if ctx and int(v) == int(ctx):
            return None
        return int(v)
    return None


def max_input_tokens(model: str):
    """The model's published input-context limit, or None if we don't know it. A request larger than this is
    REJECTED by the provider, so an estimate implying one is impossible — that is what makes this a rail and
    not a tuned threshold (see gate._implausible_estimate)."""
    if not model:
        return None
    for key in (model, normalize(model)):
        v = (CONTEXT_LIMITS.get(key) or {}).get("max_input_tokens")
        if v:
            return int(v)
    return None


def unit_price(kind: str, model: str, variant: str = None) -> float:
    """$ per unit for non-token billing. Lookup: exact `model:variant` → `model` (normalized too).
    Raises KeyError when unpriced — callers record the call at $0 with a LOUD warn (never guess)."""
    table = UNIT_PRICES.get(kind)
    if table is None:
        raise KeyError(f"unknown unit kind {kind!r}")
    for key in ((f"{model}:{variant}",) if variant else ()) + (model, normalize(model)):
        if key in table:
            return table[key]
    raise KeyError(
        f"No {kind} unit price for {model!r}"
        + (f" (variant {variant!r})" if variant else "")
        + " — add it to prices.json `unit_prices` with a source or run `spendguard sync-prices`."
    )


def providers():
    """{provider: [model, ...]} — the configured services and models."""
    out = {}
    for m, p in PROVIDERS.items():
        out.setdefault(p, []).append(m)
    return out


_OPENROUTER_URL = "https://openrouter.ai/api/v1/models"

_SYS_SAME_MODEL = (
    "You match model identifiers across two catalogs. Two ids name the SAME model only if they are the "
    "same weights from the same vendor at the same version — a dated snapshot and its floating alias are "
    "the same model. A different size, version, vendor, or modality is NOT the same model, however similar "
    "the names look. If no candidate is the same model, say so; a wrong match is worse than no match.")


def _same_model_as_ours(ours, their_ids, run=False, advisor=None):
    """{our_id: their_id} for ids that name the SAME model. AGENTIC, and the judgement is RECORDED.

    EXACT EQUALITY IS APPLIED FIRST and is the only mechanical rule here, because it is the only one that
    is provably total: two identical ids are the same model, always. Everything after that is a judgement
    about two vendors' naming conventions, and the previous implementation made it by deleting every
    non-alphanumeric character and comparing what was left:

        re.sub(r"[^a-z0-9]", "", model.lower())

    That rule is wrong in BOTH directions and neither failure announces itself:
      FALSE MATCH  `gpt-4o` and `gpt-4-o` both collapse to `gpt4o`, so two different price cards get
                   compared and the difference is reported as DRIFT in our own table.
      FALSE MISS   `gpt-5.5-2026-01-01` never equals `gpt-5.5`, so real drift is counted as "not on
                   OpenRouter" — reported as COVERAGE, which reads like nothing is wrong.
    It also took `id.split("/")[-1]`, discarding OpenRouter's vendor prefix: the same bare-name collision
    this module was just fixed for on its own side, applied to somebody else's catalog.

    Judged once and stored as a per-model fact, so it is never re-paid. models.py is THE per-model store;
    this does not open a second one. An empty stored value means "judged: no counterpart exists" — which is
    a real answer and must not be confused with "not yet asked"."""
    from . import adapters, calls, config, models as _models, ui
    theirs = set(their_ids)
    out, unresolved = {}, []
    for m in ours:
        if m in theirs:
            out[m] = m                                   # identical ids: provable, free, no judgement
            continue
        rec = _models.facts(m).get("openrouter_id")
        if rec is not None:
            if rec[0]:
                out[m] = rec[0]
            continue                                     # "" = judged, no counterpart. Not "unasked".
        unresolved.append(m)
    stats = {"exact": len(out), "agentic": 0, "unresolved": len(unresolved)}
    if not unresolved:
        return out, stats

    model = advisor or config.advisor_model()
    prompt = ("Our catalog ids:\n" + "\n".join(f"  {m}" for m in unresolved)
              + "\n\nCandidate ids from the other catalog:\n" + "\n".join(f"  {t}" for t in sorted(theirs))
              + '\n\nFor each of OUR ids, give the candidate that names the same model, or null.\n'
                'Reply JSON only: {"matches": [{"ours": "<id>", "theirs": "<id or null>"}]}')
    if not run:
        ui.estimate_only(action=f"resolve {len(unresolved)} model ids against the other catalog",
                         cost=realtime_cost(model, max(1, len(_SYS_SAME_MODEL + prompt) // 4),
                                            30 * len(unresolved)))
        return out, stats
    with calls.context(intent="spendguard:resolve-model-identity"):
        r = adapters.call(model, prompt, max_tokens=30 * len(unresolved) + 400, system=_SYS_SAME_MODEL)
    if r.get("error"):
        return out, stats                                # unresolved stays unresolved — never guessed
    try:
        # regex PARSES the JSON envelope out of the reply. It decides nothing: the model already did.
        blob = re.search(r"\{.*\}", r.get("text") or "", re.S)
        matches = (json.loads(blob.group(0)).get("matches") if blob else []) or []
    except Exception:
        matches = []
    for it in matches:
        ours_id, theirs_id = (it or {}).get("ours"), (it or {}).get("theirs")
        if ours_id not in set(unresolved):
            continue
        if theirs_id and theirs_id not in theirs:
            continue                                     # a name it invented is not an answer
        _models.add_fact(ours_id, "openrouter_id", theirs_id or "", confidence=0.9,
                         source=f"agentic id-resolution vs openrouter ({model})", verified=False)
        if theirs_id:
            out[ours_id] = theirs_id
            stats["agentic"] += 1
    stats["unresolved"] = len(unresolved) - stats["agentic"]
    return out, stats


def cross_check_openrouter(run=False, scope=None):
    """Price cross-check against OpenRouter's public models JSON. (rows, matched, total).

    TWO JUDGEMENTS, BOTH AGENTIC. Which of their ids names the same model as ours (_same_model_as_ours),
    and whether a rate gap means our number needs re-verifying (_judge_price_gaps). Everything mechanical
    here is provable: fetching, exact id equality, and comparing two published numbers for inequality.

    Neither judgement was agentic before. Identity was `re.sub(r"[^a-z0-9]", "", id.split("/")[-1])`, and
    significance was `> 0.10`. Both are wrong in the direction that reads as healthy — a false miss and a
    sub-threshold gap both render as "nothing to see".

    `scope` bounds which of OUR ids get a paid identity resolution when they do not match exactly; it
    defaults to the curated table, the prices we publish and are answerable for. Exact matching still runs
    across the whole table for free, and what stayed unresolved is returned rather than hidden.

    `run=False` makes this entirely zero-spend: both agentic steps report an estimate and decide nothing."""
    import json as _json, urllib.request
    from . import config
    req = urllib.request.Request(_OPENROUTER_URL, headers={"User-Agent": "spendguard/0.1"})
    data = _json.loads(urllib.request.urlopen(req, context=config.ssl_context(), timeout=10).read())
    ormap = {}
    for m in data.get("data", []):
        p = m.get("pricing") or {}
        try:
            # THE ID IS KEPT WHOLE. This used to be re.sub(r"[^a-z0-9]", "", id.split("/")[-1].lower()) —
            # discarding OpenRouter's vendor prefix and then mangling what was left, which is the same
            # bare-name collision this module was just fixed for on its OWN side, applied to somebody
            # else's catalog. Identity is decided by _same_model_as_ours, not by what survives a strip.
            ormap[m["id"]] = (float(p.get("prompt", 0)) * 1e6, float(p.get("completion", 0)) * 1e6)
        except Exception:
            pass
    exact = {m: m for m in PRICING if m in ormap}     # identical ids: provable, free, across the whole table
    ours = list(scope) if scope is not None else list(_FALLBACK)
    todo = [m for m in ours if m not in exact]
    resolved, id_stats = _same_model_as_ours(todo, ormap.keys(), run=run)
    pairs = {**exact, **resolved}
    id_stats["exact"] = len(exact)
    rows, differs = [], []
    for model, their in sorted(pairs.items()):
        pr = PRICING.get(model)
        if not pr:
            continue
        oin, oout = ormap[their]
        if (oin, oout) == (pr["in_"], pr["out"]):
            # EXACT EQUALITY, not a tolerance. The two catalogs publish the identical number, so there is
            # no gap for anyone to judge the significance of. Every row that differs by ANY amount goes to
            # the judge below — nothing is filtered out by a size test before the decision is made.
            rows.append((model, pr["in_"], oin, pr["out"], oout, "identical"))
        else:
            differs.append((model, pr["in_"], oin, pr["out"], oout))
    # WHETHER A GAP MATTERS IS A JUDGEMENT, AND IT IS MADE BY A MODEL.
    #
    # This used to read `"DRIFT" if (din > tol or dout > tol)`. The arithmetic is provable — |a-b|/b has
    # exactly one answer — but the LABEL is not: 0.10 is a hand-picked significance bar standing in for a
    # question that depends on context we have and it does not. A 9% gap on a $30/1M output rate is $2.70
    # per million tokens and is under the bar; a 12% gap on a $0.02 rate is nothing and is over it. Worse,
    # the other catalog is not always comparing like with like — a reseller's markup, a different modality,
    # or a stale listing all produce a gap that is not our price being wrong.
    #
    # So the mechanical step now decides only what is PROVABLE — do the two numbers differ at all — and
    # every row that differs is sent to the judge. That is complete by construction: nothing is filtered
    # out by a threshold before the decision, which is the failure mode a recall filter set at 0.10 had.
    if differs:
        rows.extend(_judge_price_gaps(differs, run=run))
    return rows, len(rows), len(PRICING)


_SYS_PRICE_GAP = (
    "You audit a pricing table against a third-party catalog. For each row you are given OUR published "
    "rate and THEIRS, in USD per 1M tokens. Decide whether the gap means OUR rate is likely WRONG or STALE "
    "and should be re-verified against the vendor's own page. A gap is NOT evidence our rate is wrong when "
    "the other catalog is plausibly quoting a reseller markup, a different modality or context tier, or a "
    "stale listing. Judge each row on the size of the gap RELATIVE TO THE MONEY IT MOVES, not on a fixed "
    "percentage.")


def _judge_price_gaps(differs, run=False, advisor=None):
    """[(model, our_in, their_in, our_out, their_out, verdict)] — verdict decided by a model.

    Estimate-first and caged, like every other paid step here. On refusal or error the verdict is
    "undecided", never "ok": a judge that did not answer is not a judge that cleared the row, and recording
    it as clean is how a real price drift becomes a green dashboard."""
    from . import adapters, calls, config, ui
    model = advisor or config.advisor_model()
    listing = "\n".join(f"  {i}. {m}: ours in ${a}/out ${c} | theirs in ${b}/out ${d}"
                        for i, (m, a, b, c, d) in enumerate(differs))
    prompt = (listing + '\n\nReply JSON only: {"rows": [{"i": <index>, "recheck": true|false, '
                        '"why": "<short>"}]}')
    if not run:
        ui.estimate_only(action=f"judge {len(differs)} price gaps against the other catalog",
                         cost=realtime_cost(model, max(1, len(_SYS_PRICE_GAP + prompt) // 4), 40 * len(differs)))
        return [(*r, "unjudged (estimate only)") for r in differs]
    with calls.context(intent="spendguard:judge-price-drift"):
        r = adapters.call(model, prompt, max_tokens=40 * len(differs) + 400, system=_SYS_PRICE_GAP)
    verdicts = {}
    if not r.get("error"):
        try:
            # regex PARSES the JSON envelope. The decision inside it was made by the model.
            blob = re.search(r"\{.*\}", r.get("text") or "", re.S)
            for it in (json.loads(blob.group(0)).get("rows") if blob else []) or []:
                verdicts[int(it["i"])] = ("RECHECK" if it.get("recheck") else "ok", (it.get("why") or "")[:60])
        except Exception:
            verdicts = {}
    out = []
    for i, row in enumerate(differs):
        v, why = verdicts.get(i, ("undecided", "judge did not answer"))
        out.append((*row, f"{v} — {why}" if why else v))
    return out


def freshness(today=None):
    """(verified_date, days_old, is_stale) — is the price table older than stale_after_days?"""
    import datetime
    today = today or datetime.date.today()
    try:
        v = datetime.date.fromisoformat(PRICING_VERIFIED)
        days = (today - v).days
        return PRICING_VERIFIED, days, days > STALE_AFTER_DAYS
    except Exception:
        return PRICING_VERIFIED, None, False


def normalize(model: str) -> str:
    """Map a model id to its canonical priced base. Strips, in order: a trailing date snapshot (-YYYY-MM-DD OpenAI
    or -YYYYMMDD Anthropic), a '-latest' alias, and the '-codex' coding variant. Codex variants (gpt-5.5-codex,
    gpt-5-codex) bill at their base GPT's published token rates — OpenAI prices codex at the base model — so this
    is a verified alias, not a guess; an explicit PRICING entry still wins (see price())."""
    if model is None:
        raise ValueError("model is None")
    m = model.strip()
    if m.startswith("ft:"):
        # fine-tuned ids are ft:BASE[:org][::job] — the PRICE identity is ft:<canonical BASE>. The org/job
        # suffix never changes the rate; the ft prefix ALWAYS does (ft inference bills above base, so
        # resolving to the bare base would systematically under-price — see price()).
        base = m.split(":", 2)[1]
        return "ft:" + normalize(base)
    m = re.sub(r"-\d{4}-\d{2}-\d{2}$|-\d{8}$", "", m)
    m = re.sub(r"-latest$", "", m)
    m = re.sub(r"-codex$", "", m)
    return m


def _vendor_qualified(m, provider=None, exact_only=False):
    """Resolve a BARE model id against the LiteLLM breadth layer, which keys most non-first-party models as
    `vendor/model` (`moonshot/kimi-k2.5`, `zai/glm-4.6`) while callers pass the bare id their SDK takes.
    Without this the whole breadth layer is unreachable for those vendors — every GLM/Kimi call raised
    'no canonical price' even though a real published rate was sitting in the cache.

    `provider` (when the caller knows it) pins the FIRST-PARTY vendor: exact, no inference. Without it, scan
    for `*/m` candidates, restricted to single-slash vendor-level keys — deep reseller paths
    (`bedrock/ap-south-1/…`, `cloudflare/@cf/…`) are a DIFFERENT vendor's resale rate, not this call's price.
    Candidates that disagree on $ raise (ambiguous → pass provider=); identical $ is safe to use. Never a guess:
    every value returned is a published rate from the synced source."""
    if provider:
        k = f"{provider}/{m}"
        if k in PRICING:
            return PRICING[k]
        if exact_only:
            # The caller NAMED a vendor and that vendor does not publish this id. Scanning other vendors
            # for it would answer a question nobody asked, so say nothing and let the caller's own
            # fallbacks run. Used by price() to consult the named provider FIRST without importing this
            # function's cross-vendor inference along with it.
            return None
    cands = {k: v for k, v in PRICING.items() if k.endswith("/" + m) and k.count("/") == 1}
    if not cands:
        return None
    rates = {_rate_key(v) for v in cands.values()}
    if len(rates) > 1:
        raise KeyError(
            f"Ambiguous price for {m!r}: vendors {sorted(k.split('/')[0] for k in cands)} publish DIFFERENT "
            f"rates. Pass the provider (`provider:{m}` / provider=) so the right one is billed — spendguard "
            f"will not pick for you."
        )
    return next(iter(cands.values()))


def price(model: str, provider: str = None) -> dict:
    # A NAMED PROVIDER IS ANSWERED BY THAT PROVIDER, before any bare-name lookup.
    #
    # Two fast paths used to reach a bare PRICING[model] first and return whatever single rate happened to
    # be stored under that name. The second one did it AFTER splitting `provider:model` — parsing the
    # vendor out of the caller's own argument and then discarding it. So `moonshot:some-shared-id` could
    # be billed at another vendor's rate, and _vendor_qualified's ambiguity guard below (which RAISES
    # rather than pick between disagreeing vendors) never got the chance to fire.
    #
    # In an accounting tool the wrong vendor's rate is not a near-miss. It is the number every downstream
    # total is built from, and nothing about it looks wrong.
    if ":" in model and not model.startswith("ft:"):
        provider, model = model.split(":", 1)     # 'provider:model' form carries its own answer — KEEP it
    if provider:
        v = _vendor_qualified(model, provider, exact_only=True)
        if v:
            return v                              # exact_only: the named vendor answers, or nobody does
    if model in PRICING:                          # an explicit verified entry (codex/dated/o-series) wins
        return PRICING[model]
    v = _vendor_qualified(model, provider)        # RAW first: `kimi-latest` is a real id, not a '-latest' alias
    if v:
        return v
    m = normalize(model)
    if m in PRICING:
        return PRICING[m]
    v = _vendor_qualified(m, provider)            # normalized (dated snapshot stripped) against the same layer
    if v:
        return v
    if m.startswith("ft:"):
        # the LiteLLM breadth layer keys fine-tunes by DATED base (ft:gpt-4o-mini-2024-07-18) — accept a
        # dated variant of the same base. NEVER fall back to the bare base price: ft inference bills higher,
        # and a silent under-price is worse than the loud $0-and-WARN the caller gets from this KeyError.
        for k in PRICING:
            if k.startswith(m + "-"):
                return PRICING[k]
        raise KeyError(
            f"No price for fine-tuned model {model!r} (looked for {m!r} and dated variants). "
            f"Run `spendguard sync-prices` (the LiteLLM layer carries ft rates) or add it with a source — "
            f"the BASE price is NOT a substitute (ft inference bills above base)."
        )
    raise KeyError(
        f"No canonical price for model {model!r} (normalized {m!r}, vendor-qualified lookups tried). "
        f"Run `spendguard sync-prices` (2,400+ models incl. most vendor-hosted ids), or add it to prices.json "
        f"WITH A SOURCE — DO NOT guess a price."
    )


def _cost(model, in_tok, out_tok, cached_in_tok, batch, provider=None):
    p = price(model, provider)
    if batch:
        pin, pout, pcache = p["batch_in"], p["batch_out"], p.get("cached_in", 0.0) * 0.5
    else:
        pin, pout, pcache = p["in_"], p["out"], p.get("cached_in", 0.0)
    cached_in_tok = min(max(0, cached_in_tok), in_tok)   # cached can't exceed (or precede) the input
    fresh_in = in_tok - cached_in_tok
    return (fresh_in * pin + cached_in_tok * pcache + out_tok * pout) / 1_000_000


def batch_cost(model: str, in_tok: int, out_tok: int = 0, cached_in_tok: int = 0, provider: str = None) -> float:
    """Actual/forecast cost ($) of `in_tok`+`out_tok` via the Batch API (50% off). `provider` pins the vendor
    when the model id is bare and several vendors host it (see _vendor_qualified)."""
    return _cost(model, in_tok, out_tok, cached_in_tok, batch=True, provider=provider)


def realtime_cost(model: str, in_tok: int, out_tok: int = 0, cached_in_tok: int = 0, provider: str = None) -> float:
    """Actual/forecast cost ($) of `in_tok`+`out_tok` for a real-time call. `provider` pins the vendor when the
    model id is bare and several vendors host it (see _vendor_qualified)."""
    return _cost(model, in_tok, out_tok, cached_in_tok, batch=False, provider=provider)


def estimate(model: str, n: int, avg_in: int, avg_out: int, batch: bool = True) -> float:
    """Pre-flight cost ($) for `n` requests of `avg_in`/`avg_out` tokens each."""
    return _cost(model, n * avg_in, n * avg_out, 0, batch=batch)


def verify():
    """Self-check the few rates that have actually burned us. Uses explicit raises, NOT assert — this guards the money
    core, and `python -O` strips asserts, which would silently skip the price sanity-check exactly when it matters."""
    def _need(cond, msg):
        if not cond:
            raise ValueError("pricing.verify: " + msg)
    _need(price("gpt-5.5")["batch_in"] == 2.50 and price("gpt-5.5")["batch_out"] == 15.00, "gpt-5.5 batch wrong")
    _need(price("gpt-5.5")["in_"] == 5.00 and price("gpt-5.5")["out"] == 30.00, "gpt-5.5 realtime wrong")
    _need(normalize("gpt-5.5-2026-04-23") == "gpt-5.5", "gpt-5.5 normalize wrong")
    return True


def main():
    verify()
    print(f"Canonical OpenAI pricing  (source: {PRICING_SOURCE}, verified {PRICING_VERIFIED})")
    print(f"{'model':<24}{'rt_in':>8}{'rt_out':>9}{'batch_in':>10}{'batch_out':>11}")
    for m, p in PRICING.items():
        print(f"{m:<24}{p['in_']:>8.3f}{p['out']:>9.3f}{p['batch_in']:>10.3f}{p['batch_out']:>11.3f}")
    print("\nself-check: OK  (gpt-5.5 batch = $2.50 in / $15.00 out per 1M)")
    # worked example so the magnitude is obvious
    print(f"example: 1M in + 1M out gpt-5.5 batch = ${batch_cost('gpt-5.5', 1_000_000, 1_000_000):.2f}"
          f"  (NOT ${(1.25+10.0):.2f} as old scripts assumed)")
    return 0


if __name__ == "__main__":
    main()

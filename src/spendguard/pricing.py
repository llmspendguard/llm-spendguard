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
import re, json
from typing import Optional

# Authored multipliers and provenance live with all other price data in the catalog.
from . import model_catalog as _price_catalog

_ANTHROPIC_CACHE_MODIFIERS = _price_catalog.cache_modifier_table()["anthropic"]
CACHE_WRITE_5M_MULTIPLIER = _ANTHROPIC_CACHE_MODIFIERS["write_5m"]
CACHE_WRITE_1H_MULTIPLIER = _ANTHROPIC_CACHE_MODIFIERS["write_1h"]
CACHE_READ_MULTIPLIER = _ANTHROPIC_CACHE_MODIFIERS["read"]

ONE_MILLION = 1_000_000          # rates are quoted per this many tokens; a UNIT, never a workload
PRICING_SOURCE = "https://developers.openai.com/api/docs/pricing"
# Re-verified 2026-09-12 against the VENDOR pages (not just OpenRouter): OpenAI 7/7 exact (gpt-5 1.25/10,
# gpt-5-mini 0.25/2, gpt-5-nano 0.05/0.40, gpt-5.4 2.50/15, gpt-5.5 5/30, gpt-4o 2.50/10, gpt-4o-mini 0.15/0.60)
# and Anthropic 3/3 exact (opus 5/25, sonnet-5 2/10, haiku-4.5 1/5). The claude-sonnet-5 "reverts to 3/15 on
# 2026-09-01" note did NOT come true — the vendor page still shows 2/10, so the introductory rate held (the synced
# table keeps the correct 2/10; sync-prices owns that note). The 14 SECONDARY synced-layer gaps were agentically
# judged ($0 on-lane): 10 ok, deepseek-v4-pro correct (our 1.32/3.96 = the vendor PEAK rate), deepseek-v4-flash
# RETIRED upstream (→ deepseek-flash; sync-prices will refresh it), qwen-plus pending a DashScope-console check.
PRICING_VERIFIED = "2026-09-12"
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
    """Compatibility accessor for the catalog authoring path."""
    from . import model_catalog
    return str(model_catalog.catalog_authoring_path())


def set_price(model, provider, in_usd, out_usd, source, batch_in=None, batch_out=None, cached_in=None,
              verified=None):
    """Author a catalog price with mandatory source and verification provenance."""
    from .catalog_price_authoring import set_catalog_model_price
    return set_catalog_model_price(model, input=in_usd, output=out_usd, cached=cached_in,
                                   batch_input=batch_in, batch_output=batch_out, provider=provider,
                                   source=source, verified=verified)


LITELLM_BREADTH_FILENAME = "litellm_prices.json"


def _load_pricing_breadth_snapshot():
    """Read optional synced breadth only in the designated price loader."""
    from . import config
    path = config.HOME / LITELLM_BREADTH_FILENAME
    if not path.exists():
        return {}
    try:
        with path.open() as handle:
            return json.load(handle)
    except (OSError, ValueError) as error:
        import sys
        sys.stderr.write(f"[pricing] GAP unreadable breadth cache {path}: {error}\n")
        return {}


PRICING_BREADTH_SNAPSHOT = _load_pricing_breadth_snapshot()


def _derive_catalog_pricing_state(snapshot=None):
    """Derive rate, provider and ambiguity maps in memory; catalog overrides breadth for its vendor."""
    from . import model_catalog
    snapshot = PRICING_BREADTH_SNAPSHOT if snapshot is None else snapshot
    providers = dict(snapshot.get("providers") or {})
    prices = {model: dict(row) for model, row in (snapshot.get("models") or {}).items()}
    ambiguous = set()
    for provider, rows in model_catalog.as_price_table().items():
        for model, row in rows.items():
            qualified = f"{provider}/{model}"
            rates = {**prices.get(qualified, {}), **row, "provider": provider}
            prices[qualified] = rates
            previous = prices.get(model)
            if previous is not None and providers.get(model) != provider and _rate_key(previous) != _rate_key(rates):
                prices.pop(model, None)
                ambiguous.add(model)
                providers.pop(model, None)
            elif model not in ambiguous:
                prices[model] = rates
                providers[model] = provider
    return prices, providers, ambiguous


def _load():
    """Compatibility loader returning the derived price table."""
    return _derive_catalog_pricing_state(_load_pricing_breadth_snapshot())[0]


PRICING, PROVIDERS, AMBIGUOUS_BARE = _derive_catalog_pricing_state()


# ── MAXIMUM OUTPUT TOKENS, per model ──────────────────────────────────────────────────────────────────
# Why this exists: the send-time OUTPUT budget floors at adapters.TOKEN_FLOOR (32k OUTPUT tokens — never an
# input bound) because a cap has never controlled cost and a low one destroys the answer. But a floor above a
# model's own OUTPUT limit is a 400, so the floor has to be clamped by what the model actually accepts. This is
# the OUTPUT axis only; the INPUT axis is max_input_tokens, and the two never constrain each other.
#
# The per-model published OUTPUT ceiling now lives in the MODEL CATALOG — the one version-controlled home
# (concern 'model_catalog', model_catalog.published_ceiling), each value sourced from the provider's own docs.
# max_output_tokens() reads it there. (A MAX_OUT dict for Anthropic + an _OUTPUT_CEILING_OVERRIDES dict for GLM
# used to live here; both were migrated INTO model_catalog.json on 2026-09-25 so a ceiling is authored in exactly
# ONE place.) A model ABSENT from the catalog resolves to None → the caller uses the floor and self-heals, the
# same unknown≠guess rule as unpriced ≠ $0.


def max_output(model):
    """A LEARNED max-output fact for this model, or None. The published/documented ceiling is NOT here any more —
    it is in the model catalog and reached via max_output_tokens() (which output_ceiling consults FIRST). This
    function is only the learned-fact tier: a value the adapter recorded after a provider refused a larger budget,
    or pulled from a live /models endpoint. None → the caller uses the floor and self-heals; unknown is unknown."""
    try:
        from . import models as _m
        fact = _m.facts(normalize(model)).get("max_output_tokens")
        if fact and int(fact[0]) > 0:
            return int(fact[0])
    except Exception:
        pass                      # no fact store / not yet learned
    return None


def _load_units():
    """UNIT_PRICES {kind: {model[:variant]: usd}} for non-token billing — kinds: `image` (per image,
    variant 'WxH:quality'), `audio_second` (transcription $/second), `tts_char` ($/character),
    `training_token` (fine-tune training $/token). Layered like PRICING, lowest→highest:
    LiteLLM cache per-unit fields (input_cost_per_second / *_per_character / *_per_image — provenance:
    the synced upstream table)  →  catalog `unit_prices`. NOTHING is guessed: an absent
    unit price surfaces as a loud unpriced-call WARN at the gate, never a made-up number."""
    units = {"image": {}, "audio_second": {}, "tts_char": {}, "training_token": {}, "web_search_call": {}}
    for model, row in (PRICING_BREADTH_SNAPSHOT.get("unit_models") or {}).items():
        if row.get("input_cost_per_second"):
            units["audio_second"][model] = float(row["input_cost_per_second"])
        per_char = row.get("input_cost_per_character") or row.get("output_cost_per_character")
        if per_char:
            units["tts_char"][model] = float(per_char)
        per_image = row.get("output_cost_per_image") or row.get("input_cost_per_image")
        if per_image:
            units["image"][model] = float(per_image)
    from . import model_catalog
    for kind, entries in model_catalog.unit_price_table().items():
        if kind in units:
            units[kind].update({key: float(value) for key, value in entries.items()})
    return units


UNIT_PRICES = _load_units()


def _load_context():
    """{model: {max_input_tokens, max_output_tokens}} from the synced LiteLLM cache. These are LIMITS, not
    prices — the upstream table is `model_prices_and_context_window.json` and carries both. Absent → {}, and
    every caller must treat "unknown" as "no opinion" rather than inventing a bound."""
    return dict(_load_pricing_breadth_snapshot().get("context") or {})


CONTEXT_LIMITS = _load_context()

def max_output_tokens(model: str):
    """The model's published OUTPUT ceiling (tokens), or None. This is the OUTPUT axis, INDEPENDENT of
    max_input_tokens: it bounds the REPLY size and is never reduced by how large the input was. The MODEL CATALOG
    (the version-controlled SSOT, provider-doc-sourced + poison-free) wins first; then the synced LiteLLM cache as
    breadth for models the catalog does not carry. Used as the per-model output ceiling by output_ceiling/output_budget,
    and (for providers that REQUIRE the field, Anthropic) as the honest 'deliberately huge' value, never an invented one."""
    if not model:
        return None
    try:
        from . import model_catalog as _mc               # the model catalog — the SSOT for published ceilings
        cv = _mc.published_ceiling(model)                # (Anthropic MAX_OUT + GLM overrides were migrated in here)
        if cv:
            return int(cv)
    except Exception:
        pass
    for key in (model, normalize(model)):              # synced LiteLLM breadth cache for models not in the catalog
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


# THE ALWAYS-FLOOR for an output CEILING. The ceiling is NOT the cost: max_output is billed on ACTUAL tokens generated,
# so a HIGH ceiling is free and only a LOW ceiling can hurt (it truncates a real answer). Therefore NO non-authoritative
# source — a poison-prone learned fact, a caller's value, a per-class recommend — may EVER set the ceiling below this.
# Only a model's genuinely PUBLISHED (or live-catalog) maximum may be lower, because there the model truly cannot do more.
OUTPUT_FLOOR = 32_000


def output_ceiling(vendor, model, backstop, learned_floor=OUTPUT_FLOOR):
    """The SINGLE authority for a model's OUTPUT ceiling (tokens) — the ONE resolution order every call path shares,
    so the paths that hand-rolled it (adapters._call_guarded, vendor_call.output_cap, gate._autotune, zai_exec) can no
    longer DRIFT. That drift was a real hole BOTH ways: a model known only via a healed fact was clamped to the backstop
    (recommend raised ABOVE the real ceiling → a 400), and — the expensive one, hit repeatedly — a POISONED-low healed
    fact (kimi-k3 auto-healed to 2000 from a transient refusal) clamped a large answer to 2000 and TRUNCATED it.

    THE CEILING IS NOT THE COST. Billing is on ACTUAL tokens, so a high ceiling costs nothing; the only failure mode is a
    ceiling set too LOW. So the learned tier is FLOORED to `learned_floor` (default OUTPUT_FLOOR=32K) for EVERY caller —
    a non-authoritative guess can raise the ceiling toward the real max but can NEVER pull it below the floor.

    Order, most authoritative first:
      1. the PUBLISHED limits cache (max_output_tokens) — synced from provider docs, poison-free; the REAL maximum, used
         as-is even if below the floor (a genuinely-small model truly cannot produce more);
      2. the vendor's LIVE /models ceiling (catalog.model_ceiling) — authoritative too, used as-is;
      3. the LEARNED fact / docs table (max_output) — auto-heal's GUESS, NOT authoritative (poison-prone), so it is
         raised to at least `learned_floor` (default OUTPUT_FLOOR): a poisoned-low fact can never truncate below the
         floor, while a healthy fact above the floor is honoured. (Pass learned_floor=0 ONLY to read the raw fact.)
      4. `backstop` — the caller's absolute cap when NOTHING knows the model, itself never below the floor.

    The provider prefix is stripped HERE ('openai:gpt-5.5' → 'gpt-5.5') so no caller can forget it and miss the cap."""
    rid = (model or "").split(":", 1)[-1]
    pub = max_output_tokens(rid)                       # 1. published cache — authoritative, the REAL maximum
    if pub:
        return int(pub)
    try:
        from . import catalog as _cat
        cat = _cat.model_ceiling(vendor, rid)          # 2. live /models — authoritative (cache-first, $0)
    except Exception:
        cat = None
    if cat:
        return int(cat)
    learned = max_output(rid)                          # 3. learned fact — NOT authoritative → floored so poison can't truncate
    if learned:
        return max(int(learned), int(learned_floor))
    return max(int(backstop), int(learned_floor))      # 4. nothing knows the model → the backstop, never below the floor


def max_input_tokens(model: str):
    """The model's published INPUT-context limit (tokens), or None if we don't know it. This is the INPUT axis,
    INDEPENDENT of max_output_tokens: it bounds how much prompt may be SENT and says nothing about the reply
    size. A request larger than this is REJECTED by the provider, so an estimate implying one is impossible —
    that is what makes this a rail and not a tuned threshold (consumers: gate._implausible_estimate on the
    estimate side, adapters._input_fits and vendor_call.call on the two call paths)."""
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
        + " — add it to model_catalog.json `unit_prices` with a source or run `spendguard sync-prices`."
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


def _same_model_as_ours(ours, their_ids, run=False, advisor=None, fact_key="openrouter_id"):
    """{our_id: their_id} for ids that name the SAME model. AGENTIC, and the judgement is RECORDED.

    `fact_key` is the per-model fact under which the resolution is stored/read, so this ONE agentic resolver serves
    more than one catalog without collision: 'openrouter_id' for the price cross-check, 'served_id' for the live
    served-catalog pre-flight (adapters dispatch). A different fact_key is a different question about the same
    model, correctly kept in its own slot — never one namespace answering for another.

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
        rec = _models.facts(m).get(fact_key)
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
        _models.add_fact(ours_id, fact_key, theirs_id or "", confidence=0.9,
                         source=f"agentic id-resolution ({fact_key}, {model})", verified=False)
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
    from . import model_catalog
    ours = list(scope) if scope is not None else list(model_catalog.all_records())
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
    # Reasoning-EFFORT suffix (…-minimal/-low/-medium/-high/-xhigh/-max/-none): effort changes how MANY tokens a
    # call spends, never the $/token RATE, so it prices at the base model. spendguard appends this suffix itself —
    # e.g. the Gemini lane's `gemini-3.7-flash-low` bills at `gemini-3.7-flash` rates. An explicit PRICING entry for
    # the suffixed id still wins (price() consults it before normalize), so a model whose real name ends this way
    # is unaffected as long as it is priced directly.
    m = re.sub(r"-(minimal|low|medium|high|xhigh|max|none)$", "", m)
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
        raise PriceUnknownError(
            f"Ambiguous price for {m!r}: vendors {sorted(k.split('/')[0] for k in cands)} publish DIFFERENT "
            f"rates. Pass the provider (`provider:{m}` / provider=) so the right one is billed — spendguard "
            f"will not pick for you. Add an authoritative price to src/spendguard/model_catalog.json (the SSOT)."
        )
    return next(iter(cands.values()))


class PriceUnknownError(KeyError):
    """No authoritative catalog price or unambiguous breadth price exists."""


CATALOG_RATE_INTERVAL_SELECTION_FIELDS = frozenset(("valid_from", "valid_until"))


def _semantic_catalog_map(model: str) -> Optional[str]:
    """Hook for a future cached semantic catalog mapping; never calls an LLM in the hot path."""
    return None


def _catalog_price(model: str, as_of=None) -> Optional[dict]:
    """Resolve exact, common variant, then alias ids using only catalog rates and providers. Returns the
    pricing-layer dict shape — provenance under `_source` (the key the breadth layer and every consumer read),
    provider from the catalog record — or None when the catalog has no USABLE rate for the id (a price_error /
    ceiling-only entry has no `in_`), so the caller falls through to the breadth/fallback path unchanged."""
    from . import model_catalog

    def _norm_price_row(p, provider):
        if not p or p.get("in_") is None:          # catalog entry with no usable rate (price_error/ceiling-only)
            return None
        row = {
            key: value for key, value in p.items()
            if key not in CATALOG_RATE_INTERVAL_SELECTION_FIELDS
        }
        if row.get("source") and not row.get("_source"):
            row["_source"] = row.pop("source")      # catalog authors `source`; the pricing layer reads `_source`
        row["provider"] = provider
        return row

    candidate = model
    while True:
        record = model_catalog.model_record(candidate) or {}
        row = _norm_price_row(model_catalog.price_for_date_from_record(record, as_of), record.get("provider"))
        if row:
            return row
        base = re.sub(r"-(?:\d{4}-\d{2}-\d{2}|\d{8}|preview|latest|v\d+)$", "", candidate)
        if base == candidate:
            break
        candidate = base
    for rec in model_catalog.all_records().values():
        if model in (rec.get("aliases") or []):
            row = _norm_price_row(model_catalog.price_for_date_from_record(rec, as_of), rec["provider"])
            if row:
                return row
    mapped = _semantic_catalog_map(model)
    if mapped:
        record = model_catalog.model_record(mapped) or {}
        row = _norm_price_row(model_catalog.price_for_date_from_record(record, as_of), record.get("provider"))
        if row:
            return row
    return None


def price(model: str, provider: str = None, *, as_of=None) -> dict:
    """The RAW per-token RATE row for `model` — {in_, out, cached_in, batch_in, batch_out, provider, …} in DOLLARS
    PER MILLION TOKENS. This is the rate TABLE, not a cost: do NOT multiply raw token counts by these values (that is
    off by 1e6 — a caller printed a $258,249 quote for a $0.26 job doing exactly that). To get DOLLARS, call
    `pricing.realtime_cost(model, in_tok, out_tok[, cached_in_tok])` or `pricing.batch_cost(...)` — or
    `cost_or_unpriced(...)` for None instead of a raise on an unpriced model. The input-rate key is `in_` (not `in`,
    which is a Python keyword). Raises KeyError for an unknown model — never guesses a price."""
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
    catalog = _catalog_price(model, as_of)
    if catalog and (not provider or catalog["provider"] == provider):
        return catalog
    if provider:
        v = _vendor_qualified(model, provider, exact_only=True)
        if v:
            return v                              # exact_only: the named vendor answers, or nobody does
    if as_of is not None:
        raise PriceUnknownError(
            f"No catalog price interval covers {model!r} on {str(as_of)[:10]}; refusing a current-rate fallback")
    if model in PRICING:                          # an explicit verified entry (codex/dated/o-series) wins
        return PRICING[model]
    # NB: a NAMED provider that doesn't publish the id falls through to the cross-vendor scan below, which resolves an
    # UNAMBIGUOUS single-vendor entry (and RAISES when vendors DISAGREE — the ambiguity guard). This lenient "a pinned
    # vendor still resolves the unambiguous entry" is intended + tested (test_pricing_vendor_ids); the disagree-case is
    # what protects against a wrong rate, not stripping the fallback.
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
        raise PriceUnknownError(
            f"No price for fine-tuned model {model!r} (looked for {m!r} and dated variants). "
            f"Run `spendguard sync-prices` (the LiteLLM layer carries ft rates) or add it with a source — "
            f"the BASE price is NOT a substitute (ft inference bills above base)."
        )
    raise PriceUnknownError(
        f"cannot price {model!r}: not in the model_catalog SSOT, no variant/alias match, and no unambiguous "
        f"fallback price. Add it to src/spendguard/model_catalog.json (the SSOT) WITH A SOURCE — DO NOT guess a "
        f"price — or run `spendguard sync-prices` if it is a vendor-hosted breadth id the LiteLLM layer carries."
    )


def _cost(model, in_tok, out_tok, cached_in_tok, batch, provider=None, cache_creation_tok=0):
    p = price(model, provider)
    if batch:
        pin, pout = p["batch_in"], p["batch_out"]
        # THE SECOND COPY OF AN INVENTED RATE, and the more consequential one: this function prices ACTUAL
        # billed usage into the ledger, not a projection. It read `cached_in * 0.5` — the same unsourced
        # stacking assumption that was fixed in estimate.project() while this one was missed, because the
        # fix was made where the bug was NOTICED rather than everywhere the expression appeared.
        #
        # `batch_cached_in` is now on the rows whose provider documents the stacking (Anthropic). Where a
        # provider does not, the rate is unknown, and unknown is charged at the full batch input rate: an
        # unpriced component must never be cheaper than reality, or real spend records as partly free —
        # the same rule as unpriced != $0. `spendguard reconcile` trues the total down against provider
        # billing, which is the mechanism for closing the gap with fact rather than with a guess.
        pcache = p.get("batch_cached_in")
        if pcache is None:
            pcache = pin
    else:
        pin, pout = p["in_"], p["out"]
        pcache = p.get("cached_in")
        if pcache is None:
            pcache = pin                 # was `0.0` — a missing cache rate billed cache reads as FREE
    cached_in_tok = min(max(0, cached_in_tok), in_tok)   # cached can't exceed (or precede) the input
    fresh_in = in_tok - cached_in_tok
    # cache CREATION (write) is a SEPARATE token class billed ~1.25x the base input rate (5-min cache) — omitting
    # it undercounts cache-heavy spend. Defaults to 0, so every existing caller is unchanged; only callers that
    # actually measure it (the realtime oracle reading Anthropic admin usage) pass it.
    ccreate_cost = max(0, cache_creation_tok) * pin * CACHE_WRITE_5M_MULTIPLIER
    return (fresh_in * pin + cached_in_tok * pcache + out_tok * pout + ccreate_cost) / 1_000_000


# THE ONE IN-PROCESS REGISTRY OF MODELS WE COULD NOT PRICE. reconcile_anthropic kept a private
# UNKNOWN_MODELS dict and report.openai_by_day kept nothing at all, so an unpriced model CRASHED the whole
# OpenAI report with a KeyError while the Anthropic path beside it degraded gracefully. Same job, two
# modules, one of them missing. Unpriced is never silently $0 — the tokens are real and the dollars are
# UNKNOWN, so the model is named here and surfaced by the report. (budget.record_unpriced is the persistent
# twin: this one is for the current process's reporting, that one marks the ledger row.)
UNPRICED_SEEN: dict = {}


def note_unpriced(model: str) -> None:
    UNPRICED_SEEN[model] = UNPRICED_SEEN.get(model, 0) + 1


def cost_or_unpriced(model: str, in_tok: int, out_tok: int = 0, cached_in_tok: int = 0,
                     batch: bool = True, provider: str = None, cache_creation_tok: int = 0) -> float:
    """Cost, or 0.0 with the model RECORDED as unpriced. Never raises on a model we have no card for.
    `cache_creation_tok` (cache-write tokens) defaults to 0 — pass it where measured so it isn't undercounted."""
    try:
        fn = batch_cost if batch else realtime_cost
        return fn(model, in_tok, out_tok, cached_in_tok, provider=provider,
                  cache_creation_tok=cache_creation_tok) or 0.0
    except (KeyError, TypeError, ValueError):
        note_unpriced(model)
        return 0.0


def batch_cost(model: str, in_tok: int, out_tok: int = 0, cached_in_tok: int = 0, provider: str = None,
               cache_creation_tok: int = 0) -> float:
    """Actual/forecast cost ($) of `in_tok`+`out_tok` via the Batch API (50% off). `provider` pins the vendor
    when the model id is bare and several vendors host it (see _vendor_qualified). `cache_creation_tok` optional."""
    return _cost(model, in_tok, out_tok, cached_in_tok, batch=True, provider=provider,
                 cache_creation_tok=cache_creation_tok)


def input_cost_lower_bound(model, in_tok, *, batch=False, provider=None,
                           cache_read_tok=None, cache_write_tok=None):
    """A provable input-charge floor, preserving unknown cache composition.

    Missing read metadata permits every input token to be discounted. Compare
    both endpoints using this module's actual pricing; absent discount prices
    cannot establish a nonzero floor. Unknown writes add no guaranteed charge.
    """
    if in_tok is None:
        return None
    floor_cost_function = batch_cost if batch else realtime_cost
    if cache_read_tok is not None:
        return floor_cost_function(model, in_tok, 0, cache_read_tok, provider=provider,
                                   cache_creation_tok=cache_write_tok or 0)
    floor_price_card = price(model, provider)
    floor_cache_price_field = "batch_cached_in" if batch else "cached_in"
    if floor_price_card.get(floor_cache_price_field) is None:
        return None
    return min(floor_cost_function(model, in_tok, 0, cached_count, provider=provider,
                                   cache_creation_tok=cache_write_tok or 0)
               for cached_count in (0, in_tok))


def realtime_cost(model: str, in_tok: int, out_tok: int = 0, cached_in_tok: int = 0, provider: str = None,
                  cache_creation_tok: int = 0) -> float:
    """Actual/forecast cost ($) of `in_tok`+`out_tok` for a real-time call. `provider` pins the vendor when the
    model id is bare and several vendors host it (see _vendor_qualified). `cache_creation_tok` (cache-write tokens,
    billed ~1.25x input) is a separate class — pass it when you have it, else it's 0 and unchanged."""
    return _cost(model, in_tok, out_tok, cached_in_tok, batch=False, provider=provider,
                 cache_creation_tok=cache_creation_tok)


def estimate(model: str, n: int, avg_in: int, avg_out: int, batch: bool = True) -> float:
    """Pre-flight cost ($) for `n` requests of `avg_in`/`avg_out` tokens each."""
    return _cost(model, n * avg_in, n * avg_out, 0, batch=batch)


def verify_pricing():
    """Self-check the few rates that have actually burned us. Uses explicit raises, NOT assert — this guards the money
    core, and `python -O` strips asserts, which would silently skip the price sanity-check exactly when it matters."""
    def _need(cond, msg):
        if not cond:
            raise ValueError("pricing.verify_pricing: " + msg)
    _need(price("gpt-5.5")["batch_in"] == 2.50 and price("gpt-5.5")["batch_out"] == 15.00, "gpt-5.5 batch wrong")
    _need(price("gpt-5.5")["in_"] == 5.00 and price("gpt-5.5")["out"] == 30.00, "gpt-5.5 realtime wrong")
    _need(normalize("gpt-5.5-2026-04-23") == "gpt-5.5", "gpt-5.5 normalize wrong")
    return True


def main():
    verify_pricing()
    print(f"Canonical OpenAI pricing  (source: {PRICING_SOURCE}, verified {PRICING_VERIFIED})")
    print(f"{'model':<24}{'rt_in':>8}{'rt_out':>9}{'batch_in':>10}{'batch_out':>11}")
    for m, p in PRICING.items():
        print(f"{m:<24}{p['in_']:>8.3f}{p['out']:>9.3f}{p['batch_in']:>10.3f}{p['batch_out']:>11.3f}")
    print("\nself-check: OK  (gpt-5.5 batch = $2.50 in / $15.00 out per 1M)")
    # worked example so the magnitude is obvious
    # ONE_MILLION is the UNIT these rates are quoted in, not an estimate of a job — this line
    # demonstrates the per-MTok rate. Named so it reads as a unit and not as a guessed workload.
    print(f"example: 1M in + 1M out gpt-5.5 batch = ${batch_cost('gpt-5.5', ONE_MILLION, ONE_MILLION):.2f}"
          f"  (NOT ${(1.25+10.0):.2f} as old scripts assumed)")
    return 0


if __name__ == "__main__":
    main()

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
import re, os, json

# per 1M tokens: realtime in/out, cached input, batch in/out  (batch == 50% of realtime).
# This dict is the FALLBACK / source-of-record for the shipped prices.json. At runtime
# prices load from config (prices.json + optional user override); if that fails, this is used.
_FALLBACK = {
    # ---- current flagship (on the live pricing page, verified 2026-06-13) ----
    "gpt-5.5":      dict(in_=5.00,  out=30.00,  cached_in=0.50,  batch_in=2.50,  batch_out=15.00),
    "gpt-5.5-pro":  dict(in_=30.00, out=180.00, cached_in=3.00,  batch_in=15.00, batch_out=90.00),
    "gpt-5.4":      dict(in_=2.50,  out=15.00,  cached_in=0.25,  batch_in=1.25,  batch_out=7.50),
    "gpt-5.4-mini": dict(in_=0.75,  out=4.50,   cached_in=0.075, batch_in=0.375, batch_out=2.25),
    "gpt-5.4-nano": dict(in_=0.20,  out=1.25,   cached_in=0.02,  batch_in=0.10,  batch_out=0.625),
    # ---- legacy (not on current page; stable historical rates) ----
    "gpt-5":        dict(in_=1.25,  out=10.00,  cached_in=0.125, batch_in=0.625, batch_out=5.00),
    "gpt-5-mini":   dict(in_=0.25,  out=2.00,   cached_in=0.025, batch_in=0.125, batch_out=1.00),
    "gpt-5-nano":   dict(in_=0.05,  out=0.40,   cached_in=0.005, batch_in=0.025, batch_out=0.20),
    "gpt-4o":       dict(in_=2.50,  out=10.00,  cached_in=1.25,  batch_in=1.25,  batch_out=5.00),
    "gpt-4o-mini":  dict(in_=0.15,  out=0.60,   cached_in=0.075, batch_in=0.075, batch_out=0.30),
    "gpt-4.1-mini": dict(in_=0.40,  out=1.60,   cached_in=0.10,  batch_in=0.20,  batch_out=0.80),
    "text-embedding-3-large": dict(in_=0.13, out=0.0, cached_in=0.0, batch_in=0.065, batch_out=0.0),
    "text-embedding-3-small": dict(in_=0.02, out=0.0, cached_in=0.0, batch_in=0.010, batch_out=0.0),
    # ---- Anthropic Claude (verified via claude-api skill, 2026-06-13). batch = 50% off;
    #      cached_in = cache-READ (~0.1x in). Cache WRITE is ~1.25x in @5min / 2x @1h (not stored here).
    #      NOTE: claude-opus-4-8 is $5/$25 — NOT the old $15/$75 (that was Opus 3/4/4.1). Opus 4.8 < gpt-5.5 on output.
    "claude-opus-4-8":   dict(in_=5.00, out=25.00, cached_in=0.50, batch_in=2.50, batch_out=12.50),
    "claude-sonnet-4-6": dict(in_=3.00, out=15.00, cached_in=0.30, batch_in=1.50, batch_out=7.50),
    "claude-haiku-4-5":  dict(in_=1.00, out=5.00,  cached_in=0.10, batch_in=0.50, batch_out=2.50),
    # legacy Claude (stable historical rates) — for reconciling older batches
    "claude-opus-4-7":   dict(in_=5.00,  out=25.00, cached_in=0.50, batch_in=2.50,  batch_out=12.50),
    "claude-opus-4-6":   dict(in_=5.00,  out=25.00, cached_in=0.50, batch_in=2.50,  batch_out=12.50),
    "claude-opus-4-5":   dict(in_=5.00,  out=25.00, cached_in=0.50, batch_in=2.50,  batch_out=12.50),
    "claude-opus-4-1":   dict(in_=15.00, out=75.00, cached_in=1.50, batch_in=7.50,  batch_out=37.50),
    "claude-opus-4-0":   dict(in_=15.00, out=75.00, cached_in=1.50, batch_in=7.50,  batch_out=37.50),
    "claude-sonnet-4-5": dict(in_=3.00,  out=15.00, cached_in=0.30, batch_in=1.50,  batch_out=7.50),
    "claude-sonnet-4-0": dict(in_=3.00,  out=15.00, cached_in=0.30, batch_in=1.50,  batch_out=7.50),
}

PRICING_SOURCE = "https://developers.openai.com/api/docs/pricing"
PRICING_VERIFIED = "2026-06-13"
STALE_AFTER_DAYS = 45
PROVIDERS = {}  # model -> provider, populated by _load


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
    PROVIDERS = {m: ("anthropic" if m.startswith("claude") else "openai") for m in _FALLBACK}
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
                    prices[model] = rates
                    PROVIDERS[model] = prov
        except Exception as e:
            import sys
            sys.stderr.write(f"[pricing] WARN could not load {path} ({e}); using built-in fallback\n")
    return prices


PRICING = _load()


def providers():
    """{provider: [model, ...]} — the configured services and models."""
    out = {}
    for m, p in PROVIDERS.items():
        out.setdefault(p, []).append(m)
    return out


_OPENROUTER_URL = "https://openrouter.ai/api/v1/models"


def cross_check_openrouter(tol=0.10):
    """Free, read-only price drift check against OpenRouter's public models JSON. Strict normalized
    match (avoid false matches); returns (rows, matched, total). Frontier models not on OpenRouter
    simply don't match — reported as coverage, not error."""
    import json as _json, urllib.request
    from . import config
    req = urllib.request.Request(_OPENROUTER_URL, headers={"User-Agent": "spendguard/0.1"})
    data = _json.loads(urllib.request.urlopen(req, context=config.ssl_context(), timeout=10).read())
    ormap = {}
    for m in data.get("data", []):
        p = m.get("pricing") or {}
        try:
            ormap[re.sub(r"[^a-z0-9]", "", m["id"].split("/")[-1].lower())] = \
                (float(p.get("prompt", 0)) * 1e6, float(p.get("completion", 0)) * 1e6)
        except Exception:
            pass
    rows = []
    for model, pr in PRICING.items():
        key = re.sub(r"[^a-z0-9]", "", model.lower())
        if key not in ormap:
            continue
        oin, oout = ormap[key]
        din = abs(oin - pr["in_"]) / pr["in_"] if pr["in_"] else 0
        dout = abs(oout - pr["out"]) / pr["out"] if pr["out"] else 0
        rows.append((model, pr["in_"], oin, pr["out"], oout, "DRIFT" if (din > tol or dout > tol) else "ok"))
    return rows, len(rows), len(PRICING)


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
    """'gpt-5.5-2026-04-23' / 'claude-haiku-4-5-20251001' -> base id. Strips a trailing
    date snapshot in either -YYYY-MM-DD (OpenAI) or -YYYYMMDD (Anthropic) form."""
    if model is None:
        raise ValueError("model is None")
    return re.sub(r"-\d{4}-\d{2}-\d{2}$|-\d{8}$", "", model.strip())


def price(model: str) -> dict:
    m = normalize(model)
    if m not in PRICING:
        raise KeyError(
            f"No canonical price for model {model!r} (normalized {m!r}). "
            f"Add it to scripts/pricing.py with a source — DO NOT guess a price."
        )
    return PRICING[m]


def _cost(model, in_tok, out_tok, cached_in_tok, batch):
    p = price(model)
    if batch:
        pin, pout, pcache = p["batch_in"], p["batch_out"], p.get("cached_in", 0.0) * 0.5
    else:
        pin, pout, pcache = p["in_"], p["out"], p.get("cached_in", 0.0)
    cached_in_tok = min(max(0, cached_in_tok), in_tok)   # cached can't exceed (or precede) the input
    fresh_in = in_tok - cached_in_tok
    return (fresh_in * pin + cached_in_tok * pcache + out_tok * pout) / 1_000_000


def batch_cost(model: str, in_tok: int, out_tok: int = 0, cached_in_tok: int = 0) -> float:
    """Actual/forecast cost ($) of `in_tok`+`out_tok` via the Batch API (50% off)."""
    return _cost(model, in_tok, out_tok, cached_in_tok, batch=True)


def realtime_cost(model: str, in_tok: int, out_tok: int = 0, cached_in_tok: int = 0) -> float:
    """Actual/forecast cost ($) of `in_tok`+`out_tok` for a real-time call."""
    return _cost(model, in_tok, out_tok, cached_in_tok, batch=False)


def estimate(model: str, n: int, avg_in: int, avg_out: int, batch: bool = True) -> float:
    """Pre-flight cost ($) for `n` requests of `avg_in`/`avg_out` tokens each."""
    return _cost(model, n * avg_in, n * avg_out, 0, batch=batch)


def verify():
    """Self-check the few rates that have actually burned us."""
    assert price("gpt-5.5")["batch_in"] == 2.50 and price("gpt-5.5")["batch_out"] == 15.00, "gpt-5.5 batch wrong"
    assert price("gpt-5.5")["in_"] == 5.00 and price("gpt-5.5")["out"] == 30.00, "gpt-5.5 realtime wrong"
    assert normalize("gpt-5.5-2026-04-23") == "gpt-5.5"
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

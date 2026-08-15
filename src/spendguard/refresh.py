"""LLM-assisted price freshness.

Uses a configured LLM *with web search* to look up CURRENT provider prices and DIFF them
against the local prices.json — so drift is caught without hand-checking pricing pages.

Opt-in. Makes ONE paid LLM call (cheap model + web search). Review the diff before --write.
Treat results as a flag-for-review, not gospel (an LLM can still misread a page).

  spendguard refresh-prices                 # look up + show diffs (no changes)
  spendguard refresh-prices --write         # apply looked-up values + bump verified date
  spendguard refresh-prices --model claude-sonnet-4-6
"""
import json, re, os, argparse, datetime
from . import pricing, config

# The call-class this lookup is measured under, and the COLD-START floor used until it has a measured p99.
# A floor is not a cap: exceeding it raises (see the stop_reason check in _llm_lookup), so an incomplete
# price table can never be written over the canonical one — which is the only outcome that actually matters
# here. The old `max_tokens=3000` was a cap with no such check, on a reply parsed as JSON.
_REFRESH_SIG = "spendguard:price-refresh"
_REFRESH_FLOOR = 8000


def _llm_lookup(models, model_id, key):
    """Ask an LLM (with web search) for current per-1M prices. Returns {model: {in_,out,batch_in,batch_out}}."""
    import anthropic
    client = anthropic.Anthropic(api_key=key)
    prompt = (
        "You are verifying LLM API prices. Using web search of OFFICIAL provider pricing pages "
        "(OpenAI, Anthropic), return the CURRENT price in USD per 1,000,000 tokens for each model below. "
        'Reply with ONLY a JSON object mapping model id to '
        '{"in_": realtime_input, "out": realtime_output, "batch_in": batch_input, "batch_out": batch_output}. '
        "Batch is typically 50% of realtime. Omit any model you cannot verify from an official source. "
        "Models: " + ", ".join(models)
    )
    # BUDGET FROM MEASUREMENT, NOT A LITERAL. This was `max_tokens=3000` on a call that asks for the CURRENT
    # price of every model and whose reply is parsed as JSON — the exact shape where truncation is silent:
    # a cut-off body loses the tail of the model list, and the prices that survive look like a complete
    # answer. It also goes direct to the vendor client (the web_search tool is not on adapters.call), so
    # none of the guards in adapters apply here and the truncation check below has to be explicit.
    from . import bulkgate
    budget = int((bulkgate.maxtokens(_REFRESH_SIG) or {}).get("recommend") or 0) or _REFRESH_FLOOR
    msg = client.messages.create(
        model=model_id, max_tokens=budget,
        tools=[{"type": "web_search_20260209", "name": "web_search"}],
        messages=[{"role": "user", "content": prompt}],
    )
    if getattr(msg, "stop_reason", None) == "max_tokens":
        raise RuntimeError(
            f"price lookup was TRUNCATED at {budget} tokens — the reply is incomplete, so the models that "
            f"did arrive are not a complete price table and must not be written over the canonical one. "
            f"Raise {__name__}._REFRESH_FLOOR or split the model list.")
    text = "".join(getattr(b, "text", "") for b in msg.content if getattr(b, "type", None) == "text")
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        raise RuntimeError("LLM returned no parseable JSON")
    return json.loads(m.group(0))


def diff(looked):
    """(model, field, local, found) for every price that differs from the local table."""
    out = []
    for model, found in looked.items():
        cur = pricing.PRICING.get(pricing.normalize(model))
        if not cur:
            out.append((model, "(new model)", None, found)); continue
        for f in ("in_", "out", "batch_in", "batch_out"):
            if f not in found:
                continue
            try:
                differs = abs(float(found[f]) - float(cur.get(f, -1))) > 1e-9
            except (TypeError, ValueError):
                # The LLM returned a non-numeric price for this field (a string/null). It can't be compared —
                # surface it as unparseable rather than let float() abort the WHOLE diff and lose every other
                # model's real changes.
                out.append((model, f + " (unparseable)", cur.get(f), found[f]))
                continue
            if differs:
                out.append((model, f, cur.get(f), found[f]))
    return out


def _write(looked):
    src = os.path.join(os.path.dirname(__file__), "prices.json")
    if os.path.exists(src):
        with open(src) as _fh:                                # closed deterministically (was a leaked handle)
            base = json.load(_fh)
    else:
        base = {"_meta": {}, "providers": {}}
    for model, found in looked.items():
        prov = pricing.PROVIDERS.get(pricing.normalize(model), "openai")
        models = base.setdefault("providers", {}).setdefault(prov, {"models": {}}).setdefault("models", {})
        models.setdefault(model, {}).update(found)
    base.setdefault("_meta", {})["verified"] = datetime.date.today().isoformat()
    target = src if os.access(os.path.dirname(src), os.W_OK) else str(config.HOME / "prices.json")
    # prices.json — the $/token table every estimate and every receipt is computed from.
    config.update_json(target, lambda _d: base)
    return target


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default="claude-haiku-4-5", help="LLM to do the lookup (cheap is fine)")
    ap.add_argument("--write", action="store_true", help="apply looked-up values to prices.json + bump verified date")
    a = ap.parse_args(argv)
    key = config.api_key("ANTHROPIC_API_KEY")
    if not key:
        print("no ANTHROPIC_API_KEY (set env, ~/.spendguard, or ./.env). Needed for the lookup."); return 1
    models = list(pricing.PRICING)
    print(f"Looking up {len(models)} models via {a.model} + web search (one paid LLM call)…")
    try:
        looked = _llm_lookup(models, a.model, key)
    except Exception as e:
        print(f"lookup failed: {e}"); return 1
    diffs = diff(looked)
    if not diffs:
        print(f"OK: local prices match all {len(looked)} verified models (local verified {pricing.PRICING_VERIFIED}).")
        return 0
    print(f"\nDIFFERENCES ({len(diffs)})  — local -> looked-up:")
    for model, f, local, found in diffs:
        print(f"  {model:<24} {f:<10} {local}  ->  {found}")
    if a.write:
        target = _write(looked)
        print(f"\nApplied to {target} + bumped verified date. Re-run `spendguard audit` and review.")
    else:
        print("\nReview above, then re-run with --write to apply. (LLM lookups can be wrong — sanity-check first.)")
    return 0

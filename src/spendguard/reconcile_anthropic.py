"""Reconcile ACTUAL Anthropic (Claude/Opus 4.8) batch spend from real billed tokens.

Anthropic's batch LIST endpoint has no usage field, so we download each ended
batch's results and sum per-result usage (priced per-result by its own model via
canonical pricing.py, cache-aware). Per-batch sums are cached locally so daily
runs only fetch NEW batches (results expire 29d after creation).

ZERO paid calls (results download is free). Exempt from the spend gate.

LIMITATION: only BATCH usage is visible. Real-time Claude calls (e.g. the LOINC
Opus judge running via ThreadPool) are NOT captured — that needs an Admin API key
(/v1/organizations/cost_report). Flagged in output.

  python scripts/reconcile_anthropic_spend.py [--since YYYY-MM-DD] [--by-day]
"""
import os, json, argparse, socket

from . import pricing
from .config import ANTHROPIC_CACHE as CACHE_PATH, api_key as _api_key, KEYS_ENV as _KEYS_ENV
CACHE_PATH = str(CACHE_PATH)
socket.setdefaulttimeout(60)


def _key():
    k = _api_key("ANTHROPIC_API_KEY")
    if not k:
        # RAISE, not sys.exit: SystemExit is a BaseException that escapes `except Exception` guards in degradable
        # callers (leak_line / `spendguard doctor`). The CLI catches RuntimeError for a clean one-line exit.
        # name the file `init` actually CREATES (keys.env). Pointing at the legacy .env sent users to write a
        # file nothing scaffolds — reported live 2026-07-16 ("I thought we moved to keys.env?").
        raise RuntimeError(f"ANTHROPIC_API_KEY not found (set it in the environment, or add it to {_KEYS_ENV})")
    return k


def _h(k):
    return {"x-api-key": k, "anthropic-version": "2023-06-01"}


def _get_text(url, k):
    """Raw body text from the Anthropic API (batch RESULTS are JSONL, not JSON). A module-level seam so the
    offline test can stub the transport; the socket itself is opened in exactly one place."""
    from . import config
    return config.api_get_text(url, _h(k))


def _get(url, k):
    """Parsed JSON from the Anthropic API. Delegates to config.api_get — this had no timeout and handed the
    caller an unread, unclosed response object."""
    from . import config
    return config.api_get(url, _h(k))


def list_batches(k):
    rows, after = [], None
    while True:
        u = "https://api.anthropic.com/v1/messages/batches?limit=100" + (f"&after_id={after}" if after else "")
        d = _get(u, k)
        rows.extend(d["data"])
        if d.get("has_more"):
            after = d["data"][-1]["id"]
        else:
            return rows


from . import pricing as _pricing        # noqa: E402
# The same dict object pricing.UNPRICED_SEEN holds — this module's private copy WAS the duplicate.
UNKNOWN_MODELS = _pricing.UNPRICED_SEEN  # model -> result count, for models missing from pricing.py (never guessed)


def _price_tokens(model, fresh_in, cread, ccreate, out):
    """Cost for a token BREAKDOWN (fresh input / cache-read / cache-creation / output), cache-aware, via
    pricing.py batch rates. Unknown/unpriceable model -> record + return 0 (never guess, never crash).

    THE shared per-token math, so _cost (per-result) and cost_by_day (per-model re-price) can never diverge on
    how cache tokens are priced — the bug that let cost_by_day silently drop all cache-token spend."""
    try:
        p = pricing.price(model)
        # THE THIRD COPY of the same unsourced rate — this one prices real Anthropic batch spend. All read the
        # published fact from the table: batch cache-read at its own rate, a 5-minute cache WRITE at 1.25x base.
        bin_, bout = p["batch_in"], p["batch_out"]
        bcache = p.get("batch_cached_in")
        if bcache is None:
            bcache = bin_                # unknown is charged at full batch input — never cheaper than reality
        ccreate_rate = bin_ * pricing.CACHE_WRITE_5M_MULTIPLIER
        return (fresh_in * bin_ + cread * bcache + ccreate * ccreate_rate + out * bout) / 1e6
    except (KeyError, TypeError, ValueError):
        UNKNOWN_MODELS[model] = UNKNOWN_MODELS.get(model, 0) + 1
        return 0.0


def _cost(model, u):
    """Cost for one result's usage dict, cache-aware. Thin wrapper over _price_tokens (Anthropic input_tokens is
    the uncached/fresh count; cache-read and cache-creation are separate)."""
    return _price_tokens(model, u.get("input_tokens", 0), u.get("cache_read_input_tokens", 0),
                         u.get("cache_creation_input_tokens", 0), u.get("output_tokens", 0))


def refresh_cache(k, cache):
    batches = list_batches(k)
    new = 0
    for b in batches:
        bid = b["id"]
        if bid in cache:
            continue
        if b.get("processing_status") != "ended" or not b.get("results_url"):
            continue
        try:
            # RESULTS ARE JSONL, NOT JSON — parsed-JSON _get would raise on the second line. Same transport
            # (timeout + SSL context + closed socket), different content type.
            lines = _get_text(b["results_url"], k).splitlines()
        except Exception as e:
            print(f"  skip {bid}: {e}", flush=True)
            continue
        by_model = {}
        cost = 0.0
        for ln in lines:
            if not ln.strip():
                continue
            try:
                obj = json.loads(ln)
            except ValueError:
                # ONE malformed JSONL line must not abort the whole cache build (this batch AND every later batch,
                # AND cost_by_day which calls refresh_cache). Skip the unparseable line; the rest still price.
                continue
            msg = obj.get("result", {}).get("message", {}) if isinstance(obj, dict) else {}
            if not msg:
                continue
            mdl = pricing.normalize(msg.get("model", "claude-opus-4-8"))
            u = msg.get("usage", {})
            c = _cost(mdl, u)
            cost += c
            m = by_model.setdefault(mdl, {"in": 0, "out": 0, "cread": 0, "ccreate": 0, "cost": 0.0})
            m["in"] += u.get("input_tokens", 0); m["out"] += u.get("output_tokens", 0)
            # store the cache-token breakdown too, so cost_by_day can RE-PRICE cache-aware — a per-model
            # in/out-only reprice silently dropped ALL cache-read/creation spend from the recomputed totals.
            m["cread"] += u.get("cache_read_input_tokens", 0)
            m["ccreate"] += u.get("cache_creation_input_tokens", 0)
            m["cost"] += c
        cache[bid] = {"created_at": b["created_at"][:10], "cost": cost, "by_model": by_model}
        new += 1
        if new % 25 == 0:
            print(f"  ...cached {new} new batches", flush=True)
    if new:
        os.makedirs(os.path.dirname(CACHE_PATH), exist_ok=True)
        # The results this summarizes are DOWNLOADED — losing the cache means re-fetching every batch
        # body to rebuild it. Atomic, with a `~` copy of the last good one.
        from . import config
        config.update_json(CACHE_PATH, lambda _d: cache)
    return new


def cost_by_day(since=None):
    """Returns (by_day:{date:$}, by_model:{model:$}). Refreshes cache first."""
    k = _key()
    if os.path.exists(CACHE_PATH):
        with open(CACHE_PATH) as _fh:                 # closed deterministically (was a leaked handle)
            cache = json.load(_fh)
    else:
        cache = {}
    refresh_cache(k, cache)
    by_day, by_model = {}, {}
    for bid, rec in cache.items():
        d = rec["created_at"]
        if since and d < since:
            continue
        # RE-PRICE from stored token sums every call (so a price fix in pricing.py corrects history without
        # re-downloading), now CACHE-AWARE via _price_tokens: new records carry the cache-read/creation breakdown
        # and are priced at their own rates; legacy records (pre-breakdown) have only in/out, so their cache
        # tokens read as 0 — no worse than the old in/out-only reprice, and new spend is now priced correctly.
        for mdl, mm in rec.get("by_model", {}).items():
            c = _price_tokens(mdl, mm.get("in", 0), mm.get("cread", 0), mm.get("ccreate", 0), mm.get("out", 0))
            by_day[d] = by_day.get(d, 0.0) + c
            by_model[mdl] = by_model.get(mdl, 0.0) + c
    return by_day, by_model


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--since")
    ap.add_argument("--by-day", action="store_true")
    a = ap.parse_args()
    by_day, by_model = cost_by_day(a.since)
    print(f"# Anthropic (Claude) BATCH spend — priced via pricing.py {pricing.PRICING_VERIFIED}")
    if a.since:
        print(f"# since {a.since} (UTC)")
    for mdl, c in sorted(by_model.items(), key=lambda x: -x[1]):
        print(f"  {mdl:<22} ${c:,.2f}")
    print(f"  {'TOTAL (batch only)':<22} ${sum(by_day.values()):,.2f}")
    print("  NOTE: real-time Claude calls NOT included (needs Admin cost_report key).")
    if UNKNOWN_MODELS:
        print("  WARN models missing from pricing.py (priced $0 — add them): "
              + ", ".join(f"{m}×{n}" for m, n in UNKNOWN_MODELS.items()))
    if a.by_day:
        for d in sorted(by_day):
            print(f"    {d}  ${by_day[d]:,.2f}")


if __name__ == "__main__":
    main()

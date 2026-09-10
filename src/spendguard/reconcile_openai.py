"""Reconcile ACTUAL OpenAI batch spend from real billed tokens.

Pulls every batch's reported token usage via the Batch API (a free GET — makes
ZERO paid calls, so it is exempt from the API-spend gate) and prices it with the
canonical scripts/pricing.py. This is the ground-truth check: run it after a
batch run to confirm estimate ~= actual, and any week to see where money went.

  python scripts/reconcile_openai_spend.py                 # all-time + per-model + cancelled waste
  python scripts/reconcile_openai_spend.py --since 2026-06-07
  python scripts/reconcile_openai_spend.py --since 2026-06-07 --by-day
  python scripts/reconcile_openai_spend.py --estimate 1500   # compare a pre-flight $ estimate to actual

KEY ACCOUNTING RULES baked in:
  * billed = COMPLETED + CANCELLED batches (cancelled bills for completed requests!)
  * failed = $0 ; in_progress/finalizing = not yet metered (reported separately)
"""
import json, argparse, urllib.request, datetime
from collections import defaultdict

from .pricing import cost_or_unpriced, normalize, PRICING_SOURCE, PRICING_VERIFIED

# Paging bounds for /v1/batches. The listing is newest-first, so a `since` window can stop early; the cap +
# cursor-advance guard + per-request timeout are what stop a stuck `has_more`/non-advancing cursor from spinning
# a scheduled report at 100% CPU forever (the observed 25-min runaway). Named, not magic — tune here, once.
BATCH_PAGE_LIMIT = 100          # OpenAI max page size
BATCH_MAX_PAGES = 1000          # hard cap: 100k batches. A non-advancing cursor terminates here even if the guard misses.
BATCH_HTTP_TIMEOUT_S = 30       # per-request socket timeout: a stalled GET fails instead of hanging the whole run.


class KeyMissing(RuntimeError):
    """No provider key configured. A RAISE (not sys.exit) so degradable callers — leak_line, `spendguard doctor`,
    signal.cancellation_rows — can swallow it via `except Exception` and degrade gracefully; the CLI catches it for
    a clean one-line exit. (sys.exit raised SystemExit, a BaseException that slipped past those `except Exception`
    guards and aborted `spendguard doctor` on a machine with no OPENAI_API_KEY.)"""


def load_key():
    from .config import api_key
    k = api_key("OPENAI_API_KEY")
    if not k:
        # name the file `init` actually CREATES (keys.env), not the legacy .env nothing scaffolds.
        from .config import KEYS_ENV
        raise KeyMissing(f"OPENAI_API_KEY not found (set it in the environment, or add it to {KEYS_ENV})")
    return k


def fetch_batches(key, since=None, max_pages=BATCH_MAX_PAGES, timeout_s=BATCH_HTTP_TIMEOUT_S):
    """Page OpenAI's /v1/batches (newest-first), BOUNDED three ways so a scheduled report can never hang:
      • since='YYYY-MM-DD' → stop once a page's OLDEST batch predates `since`; since the list is newest-first,
        no later page can be in-window. Turns an all-history pull into ~this-month (the report's actual need).
      • max_pages hard cap + a cursor-ADVANCE guard → a stuck `has_more`/non-advancing `after` (the 25-min,
        94%-CPU runaway) terminates loudly instead of spinning forever.
      • timeout_s on every request → a stalled socket fails fast instead of hanging the run.
    Returns raw batch dicts newest-first; callers still filter by day(), so a trailing partly-out-of-window page
    is harmless (the window sums are identical to an unbounded pull — this avoids work, it does not change the answer)."""
    from .config import ssl_context
    rows, after, seen = [], None, set()
    for _page in range(max_pages):
        url = f"https://api.openai.com/v1/batches?limit={BATCH_PAGE_LIMIT}" + (f"&after={after}" if after else "")
        req = urllib.request.Request(url, headers={"Authorization": f"Bearer {key}"})
        with urllib.request.urlopen(req, context=ssl_context(), timeout=timeout_s) as _r:
            d = json.load(_r)
        data = d.get("data") or []
        rows.extend(data)
        # newest-first: once the OLDEST batch on this page is before the window, stop — every later page is older.
        if since and data and day(data[-1]) < since:
            return rows
        if not d.get("has_more") or not data:
            return rows
        nxt = data[-1]["id"]
        if nxt == after or nxt in seen:                 # cursor did NOT advance → API/cursor stuck; terminate, don't spin
            _warn_paging(f"OpenAI /batches cursor did not advance (after={nxt!r}); stopping at {len(rows)} batches")
            return rows
        seen.add(nxt)
        after = nxt
    # Ran out the page cap. Silent truncation reads as "covered everything" when it did not — say so.
    _warn_paging(f"OpenAI /batches hit the {max_pages}-page cap ({len(rows)} batches); results may be truncated"
                 + (f" (window since {since})" if since else ""))
    return rows


def _warn_paging(msg):
    """Surface a paging-bound event once (stuck cursor / page-cap truncation) without taking the report down."""
    try:
        from . import config
        config.warn_once("[spendguard] " + msg)
    except Exception:
        pass


def day(b):
    return datetime.datetime.fromtimestamp(b["created_at"], datetime.timezone.utc).strftime("%Y-%m-%d")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", help="YYYY-MM-DD (UTC) lower bound on batch creation")
    ap.add_argument("--by-day", action="store_true")
    ap.add_argument("--estimate", type=float, help="a pre-flight $ estimate to compare against actual")
    a = ap.parse_args()

    rows = fetch_batches(load_key(), since=a.since)     # page-bound the pull to the window (newest-first early-stop)
    if a.since:
        rows = [b for b in rows if day(b) >= a.since]    # exact per-batch filter (the fetch bound is page-granular)

    BILLED = {"completed", "cancelled"}
    by_model = defaultdict(lambda: {"in": 0, "out": 0, "cost": 0.0, "n": 0})
    by_day = defaultdict(float)
    cancelled_waste = 0.0
    pending_req = 0
    total = 0.0
    for b in rows:
        status = b.get("status")
        if status in ("in_progress", "finalizing", "validating"):
            pending_req += (b.get("request_counts") or {}).get("total", 0) or 0   # defensive: a malformed batch
            continue                                                              # dict must not abort the reconcile
        if status not in BILLED:
            continue
        u = b.get("usage") or {}
        it, ot = u.get("input_tokens", 0), u.get("output_tokens", 0)
        if not it and not ot:
            continue
        # cost_or_unpriced, not batch_cost: an unpriced model must not abort the whole report. The Anthropic
        # reconcile has degraded gracefully since day one; this OpenAI copy never grew the guard (unpriced → 0,
        # and the model is RECORDED via note_unpriced so the gap surfaces).
        c = cost_or_unpriced(b.get("model"), it, ot, (u.get("input_tokens_details") or {}).get("cached_tokens", 0))
        m = normalize(b.get("model") or "?")
        v = by_model[m]; v["in"] += it; v["out"] += ot; v["cost"] += c; v["n"] += 1
        by_day[day(b)] += c
        if status == "cancelled":
            cancelled_waste += c
        total += c

    print(f"# OpenAI batch spend  (priced via canonical pricing.py — {PRICING_SOURCE}, verified {PRICING_VERIFIED})")
    if a.since:
        print(f"# window: created >= {a.since} (UTC)")
    print(f"\n{'model':<22}{'batches':>8}{'in_tok':>15}{'out_tok':>14}{'cost$':>12}")
    for m, v in sorted(by_model.items(), key=lambda x: -x[1]["cost"]):
        print(f"{m:<22}{v['n']:>8}{v['in']:>15,}{v['out']:>14,}{v['cost']:>12,.2f}")
    print(f"{'TOTAL BILLED':<22}{'':>8}{'':>15}{'':>14}{total:>12,.2f}")
    print(f"  of which CANCELLED-batch waste (paid, work discarded): ${cancelled_waste:,.2f}")
    if pending_req:
        print(f"  NOTE: {pending_req:,} requests in flight (not yet metered) — more cost incoming.")

    if a.by_day:
        print(f"\n{'day':<12}{'cost$':>12}")
        for d in sorted(by_day):
            print(f"{d:<12}{by_day[d]:>12,.2f}")

    if a.estimate is not None:
        diff = total - a.estimate
        ratio = (total / a.estimate) if a.estimate else float("inf")
        print(f"\nESTIMATE CHECK: estimated ${a.estimate:,.2f}  actual ${total:,.2f}  "
              f"diff ${diff:+,.2f}  ({ratio:.2f}x)")
        if ratio > 1.15 or ratio < 0.87:
            print("  *** estimate off by >15% — fix the estimator's token assumptions or model. ***")


if __name__ == "__main__":
    main()

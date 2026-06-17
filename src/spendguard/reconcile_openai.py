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
import sys, json, argparse, urllib.request, datetime
from collections import defaultdict

from .pricing import batch_cost, normalize, PRICING_SOURCE, PRICING_VERIFIED


def load_key():
    from .config import api_key
    k = api_key("OPENAI_API_KEY")
    if not k:
        sys.exit("OPENAI_API_KEY not found")
    return k


def fetch_batches(key):
    rows, after = [], None
    while True:
        url = "https://api.openai.com/v1/batches?limit=100" + (f"&after={after}" if after else "")
        req = urllib.request.Request(url, headers={"Authorization": f"Bearer {key}"})
        from .config import ssl_context
        d = json.load(urllib.request.urlopen(req, context=ssl_context()))
        rows.extend(d["data"])
        if d.get("has_more"):
            after = d["data"][-1]["id"]
        else:
            return rows


def day(b):
    return datetime.datetime.fromtimestamp(b["created_at"], datetime.timezone.utc).strftime("%Y-%m-%d")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", help="YYYY-MM-DD (UTC) lower bound on batch creation")
    ap.add_argument("--by-day", action="store_true")
    ap.add_argument("--estimate", type=float, help="a pre-flight $ estimate to compare against actual")
    a = ap.parse_args()

    rows = fetch_batches(load_key())
    if a.since:
        rows = [b for b in rows if day(b) >= a.since]

    BILLED = {"completed", "cancelled"}
    by_model = defaultdict(lambda: {"in": 0, "out": 0, "cost": 0.0, "n": 0})
    by_day = defaultdict(float)
    cancelled_waste = 0.0
    pending_req = 0
    total = 0.0
    for b in rows:
        if b["status"] in ("in_progress", "finalizing", "validating"):
            pending_req += b["request_counts"]["total"]
            continue
        if b["status"] not in BILLED:
            continue
        u = b.get("usage") or {}
        it, ot = u.get("input_tokens", 0), u.get("output_tokens", 0)
        if not it and not ot:
            continue
        c = batch_cost(b["model"], it, ot, (u.get("input_tokens_details") or {}).get("cached_tokens", 0))
        m = normalize(b["model"])
        v = by_model[m]; v["in"] += it; v["out"] += ot; v["cost"] += c; v["n"] += 1
        by_day[day(b)] += c
        if b["status"] == "cancelled":
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

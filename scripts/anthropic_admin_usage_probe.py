#!/usr/bin/env python3
"""DEV cross-check: fetch REAL Anthropic realtime API usage BY DAY from the Admin usage_report
(/v1/organizations/usage_report/messages, ANTHROPIC_ADMIN_KEY), priced via pricing.py — the ground-truth $ that the
auto-recharge credit top-ups funded. Read-only, $0 (a usage report, not an LLM call). This is the cross-check the
DISPLAY is never anchored on; it validates and (next step) feeds the local ledger.

  python scripts/anthropic_admin_usage_probe.py --since 2026-06-01
"""
import argparse
import json
import urllib.request
import urllib.parse
from collections import defaultdict

from spendguard.config import api_key
from spendguard.pricing import realtime_cost
from spendguard.resources import _norm_model


def fetch_by_day(since):
    ak = api_key("ANTHROPIC_ADMIN_KEY")
    if not ak:
        raise SystemExit("ANTHROPIC_ADMIN_KEY not set")
    by_day = defaultdict(float)
    by_tier = defaultdict(float)
    n_buckets = 0
    first_keys = None
    page = None
    for _ in range(200):
        params = [("starting_at", since + "T00:00:00Z"), ("bucket_width", "1d"), ("limit", "31"),
                  ("group_by[]", "model"), ("group_by[]", "service_tier")]
        if page:
            params.append(("page", page))
        url = "https://api.anthropic.com/v1/organizations/usage_report/messages?" + urllib.parse.urlencode(params)
        req = urllib.request.Request(url, headers={"x-api-key": ak, "anthropic-version": "2023-06-01"})
        with urllib.request.urlopen(req, timeout=90) as r:
            d = json.loads(r.read())
        for b in d.get("data", []):
            n_buckets += 1
            if first_keys is None:
                first_keys = sorted(b.keys())
            day = str(b.get("starting_at") or "")[:10]
            for rr in b.get("results", []):
                tier = (rr.get("service_tier") or "").lower()
                try:
                    c = realtime_cost(_norm_model(rr.get("model") or ""),
                                      int(rr.get("uncached_input_tokens") or rr.get("input_tokens") or 0),
                                      int(rr.get("output_tokens") or 0),
                                      cached_in_tok=int(rr.get("cache_read_input_tokens") or 0)) or 0.0
                except Exception:
                    c = 0.0
                by_day[day] += c
                by_tier[tier] += c
        if d.get("has_more") and d.get("next_page"):
            page = d["next_page"]
        else:
            break
    return by_day, by_tier, n_buckets, first_keys


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", default="2026-06-01")
    a = ap.parse_args()
    by_day, by_tier, n_buckets, first_keys = fetch_by_day(a.since)
    total = sum(by_day.values())
    print(f"Anthropic ADMIN usage_report — realtime $ priced via pricing.py, since {a.since}")
    print(f"  buckets fetched: {n_buckets}  ·  bucket keys: {first_keys}")
    print(f"  TOTAL realtime: ${total:,.2f}   (service tiers: "
          + ", ".join(f"{t or '?'} ${v:,.2f}" for t, v in sorted(by_tier.items(), key=lambda x: -x[1])) + ")")
    print("  --- by ISO week ---")
    wk = defaultdict(float)
    for day, v in by_day.items():
        if day:
            import datetime
            try:
                dt = datetime.date.fromisoformat(day)
                wk[f"{dt.isocalendar()[0]}-W{dt.isocalendar()[1]:02d}"] += v
            except Exception:
                pass
    for w in sorted(wk):
        print(f"    {w}  ${wk[w]:,.2f}")
    print("  --- recent days (last 20 with spend) ---")
    for day in sorted([d for d in by_day if by_day[d] > 0])[-20:]:
        print(f"    {day}  ${by_day[day]:,.2f}")


if __name__ == "__main__":
    main()

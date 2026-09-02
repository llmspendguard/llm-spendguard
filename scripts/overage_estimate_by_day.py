#!/usr/bin/env python3
"""Estimate REAL overage (paid tokens outside the subscription) from OBSERVABLE data: per day, the est-value of
Claude Code turns and whether that day hit the WEEKLY limit. A day with weekly-limit warnings where turns kept
SUCCEEDING = the plan cap was exhausted but usage continued on paid overage (a blocked day's turns would stop).
This is the corrected observable signal — successful-turns-past-the-weekly-wall — that the isUsingOverage-only
detector missed. Pure parse + pricing, $0."""
import os
import glob
import json
from collections import defaultdict

import spendguard.pricing as pr

P = os.path.expanduser(os.environ.get("SPENDGUARD_CC_DIR", "~/.claude/projects"))
SINCE = "2026-08-11"


def cost(u, m):
    try:
        return pr.realtime_cost(
            m,
            int(u.get("input_tokens", 0) or 0) + int(u.get("cache_creation_input_tokens", 0) or 0)
            + int(u.get("cache_read_input_tokens", 0) or 0),
            int(u.get("output_tokens", 0) or 0),
            int(u.get("cache_read_input_tokens", 0) or 0)) or 0.0
    except Exception:
        return 0.0


def main():
    day_val = defaultdict(float)
    day_turns = defaultdict(int)
    day_hits = defaultdict(int)
    day_val_after_hit = defaultdict(float)
    first_hit = {}
    seen = set()
    files = glob.glob(os.path.join(P, "**", "*.jsonl"), recursive=True)
    # pass 1: earliest weekly-limit hit per day
    for f in files:
        try:
            for line in open(f, errors="ignore"):
                if "weekly limit" not in line:
                    continue
                try:
                    o = json.loads(line)
                except Exception:
                    continue
                ts = str(o.get("timestamp", ""))
                d = ts[:10]
                if d < SINCE:
                    continue
                m = o.get("message") or {}
                c = m.get("content")
                txt = c if isinstance(c, str) else (json.dumps(c) if c else "")
                if "weekly limit" in txt:
                    day_hits[d] += 1
                    if d not in first_hit or ts < first_hit[d]:
                        first_hit[d] = ts
        except Exception:
            continue
    # pass 2: est-value per day, and est-value AFTER the day's first weekly-limit hit
    for f in files:
        try:
            for line in open(f, errors="ignore"):
                if '"usage"' not in line:
                    continue
                try:
                    o = json.loads(line)
                except Exception:
                    continue
                ts = str(o.get("timestamp", ""))
                d = ts[:10]
                if d < SINCE:
                    continue
                m = o.get("message") or {}
                u = m.get("usage")
                model = m.get("model")
                mid = m.get("id")
                if not (u and model and mid) or mid in seen:
                    continue
                seen.add(mid)
                c = cost(u, model)
                day_val[d] += c
                day_turns[d] += 1
                if d in first_hit and ts >= first_hit[d]:
                    day_val_after_hit[d] += c
        except Exception:
            continue
    print("day          est-value   turns   weekly-hits   est-value AFTER first hit (=overage candidate)")
    tot_after = 0.0
    for d in sorted(set(list(day_val) + list(day_hits))):
        after = day_val_after_hit.get(d, 0.0)
        if day_hits.get(d):
            tot_after += after
        flag = "  <= OVERAGE (usage continued past weekly wall)" if (day_hits.get(d) and after > 0) else ""
        print(f"  {d}   ${day_val.get(d,0.0):8.2f}   {day_turns.get(d,0):5d}   {day_hits.get(d,0):5d}"
              f"        ${after:8.2f}{flag}")
    print(f"\nESTIMATED overage est-value (paid tokens past the weekly wall), since {SINCE}: ${tot_after:,.2f}")
    print("  (upper bound: est-value of turns that SUCCEEDED after the weekly cap was hit that day)")


if __name__ == "__main__":
    main()

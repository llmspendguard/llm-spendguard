#!/usr/bin/env python3
"""What is OBSERVABLE in a Claude Code transcript that could distinguish plan-covered from real-billed (overflow)
usage — WITHOUT the provider admin API? Scans recent transcripts and reports every field on message.usage and the
message envelope, plus the distribution of service_tier (and any other tier/billing-looking key). Read-only, $0.

This is the grounding for calculating billing_state from OBSERVABLE data (spendguard's core identity), using the
admin API only as a later cross-check."""
import os
import glob
import json
from collections import Counter

P = os.path.expanduser(os.environ.get("SPENDGUARD_CC_DIR", "~/.claude/projects"))


def main(n_files=60):
    files = sorted(glob.glob(os.path.join(P, "**", "*.jsonl"), recursive=True),
                   key=lambda f: os.path.getmtime(f) if os.path.exists(f) else 0, reverse=True)[:n_files]
    usage_keys = Counter()
    msg_keys = Counter()
    top_keys = Counter()
    tiers = Counter()
    other_billingish = Counter()
    sample_usage = None
    sample_msg_meta = None
    n = 0
    for f in files:
        try:
            for line in open(f, errors="ignore"):
                line = line.strip()
                if not line or '"usage"' not in line:
                    continue
                try:
                    o = json.loads(line)
                except Exception:
                    continue
                m = o.get("message") or {}
                u = m.get("usage")
                if not isinstance(u, dict):
                    continue
                n += 1
                for k in o:
                    top_keys[k] += 1
                for k in m:
                    msg_keys[k] += 1
                for k in u:
                    usage_keys[k] += 1
                    if any(w in k.lower() for w in ("tier", "bill", "plan", "cost", "price", "overage", "rate")):
                        other_billingish[k] += 1
                if "service_tier" in u:
                    tiers[str(u.get("service_tier"))] += 1
                if sample_usage is None:
                    sample_usage = u
                if sample_msg_meta is None:
                    sample_msg_meta = {k: (v if k != "content" else "<...>") for k, v in m.items()}
        except Exception:
            continue
    print(f"scanned {n} usage-bearing turns across {len(files)} recent transcripts\n")
    print("message.usage keys (count):", dict(usage_keys.most_common()))
    print("service_tier distribution:", dict(tiers.most_common()))
    print("other billing-ish usage keys:", dict(other_billingish.most_common()))
    print("\nmessage envelope keys:", dict(msg_keys.most_common()))
    print("top-level record keys:", dict(top_keys.most_common()))
    print("\nsample message.usage:", json.dumps(sample_usage, indent=2))
    print("\nsample message envelope (content elided):", json.dumps(sample_msg_meta, indent=2)[:1200])


if __name__ == "__main__":
    main()

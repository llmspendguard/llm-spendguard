"""Forensic audit of the Claude Code est-value: is it double-counting?

Hypothesis: claudecode.update() scans transcripts per-FILE with a per-file watermark and sums usage on every
assistant record it sees. Claude Code resume/branch/compaction writes NEW transcript files that REPLAY earlier
messages — so the same assistant message (same message.id, same usage) can appear in several files and get counted
multiple times. This inflates est-value for heavy, resumed sessions.

This script re-derives the est-value two ways — summed over ALL usage records (what update() effectively does) vs
deduped by message.id (each API response counted ONCE) — and reports the inflation. Zero spend, read-only.
"""
import collections
import glob
import json
import os

from spendguard import claudecode


def main():
    pdir = claudecode._projects_dir()
    seen = collections.Counter()          # message.id -> times seen with usage
    once_cost = {}                          # message.id -> cost (first occurrence)
    total_cost = 0.0                        # sum over ALL records (the update() behaviour)
    dup_cost = 0.0                          # cost attributable to REPEAT occurrences (the double-count)
    recs = 0
    by_month_total = collections.defaultdict(float)
    by_month_dedup = collections.defaultdict(float)

    for path in glob.glob(os.path.join(pdir, "**", "*.jsonl"), recursive=True):
        try:
            f = open(path, errors="ignore")
        except OSError:
            continue
        with f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    r = json.loads(line)
                except Exception:
                    continue
                msg = r.get("message") or {}
                u = msg.get("usage") or {}
                mid = msg.get("id")
                model = msg.get("model")
                if not (u and model and mid):
                    continue
                recs += 1
                cost = claudecode._row_cost(model, u)[0]
                month = (r.get("timestamp") or "")[:7]
                total_cost += cost
                by_month_total[month] += cost
                if seen[mid] == 0:
                    once_cost[mid] = cost
                    by_month_dedup[month] += cost
                else:
                    dup_cost += cost
                seen[mid] += 1

    dedup_cost = sum(once_cost.values())
    dups = {k: v for k, v in seen.items() if v > 1}
    rep = collections.Counter(seen.values())
    print("Claude Code est-value audit")
    print(f"  usage records scanned:        {recs:,}")
    print(f"  distinct message ids:         {len(seen):,}")
    print(f"  ids appearing >1x (replayed): {len(dups):,}")
    print(f"  repeat distribution (seen_n: #ids): {dict(sorted(rep.items())[:8])}")
    print(f"  est-value summed over ALL records (current update() behaviour): ${total_cost:,.2f}")
    print(f"  est-value deduped by message.id (each response ONCE):           ${dedup_cost:,.2f}")
    infl = total_cost / dedup_cost if dedup_cost else 1.0
    print(f"  DOUBLE-COUNTED: ${dup_cost:,.2f}   →   {infl:.2f}x inflation")
    print("  by month (total → dedup):")
    for m in sorted(set(by_month_total) | set(by_month_dedup)):
        if by_month_total[m] > 0:
            print(f"     {m or '?'}: ${by_month_total[m]:,.2f} → ${by_month_dedup[m]:,.2f}")


if __name__ == "__main__":
    main()

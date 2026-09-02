#!/usr/bin/env python3
"""Reconcile the claude-code ingest total against the shipped `claude-code show` total.

The ledger ingest (ingest_events) requires a stable message.id per turn (its dedup_key), so it SKIPS any usage-bearing
record that has no message.id. `show()` counts those. This probe measures exactly that slice — mid-bearing deduped
est-value vs mid-LESS est-value — so a ~13% gap is EXPLAINED, not hand-waved. Pure parse + pricing, $0, no network."""
import os, glob, json
import spendguard.pricing as pr

P = os.path.expanduser("~/.claude/projects")


def cost(u, model):
    try:
        return pr.realtime_cost(
            model,
            int(u.get("input_tokens", 0) or 0) + int(u.get("cache_creation_input_tokens", 0) or 0)
            + int(u.get("cache_read_input_tokens", 0) or 0),
            int(u.get("output_tokens", 0) or 0),
            int(u.get("cache_read_input_tokens", 0) or 0)) or 0.0
    except Exception:
        return 0.0


def main():
    seen = set()
    mid_val = 0.0
    nomid_val = 0.0
    nomid_n = 0
    nomid_models = {}
    for p in glob.glob(os.path.join(P, "**", "*.jsonl"), recursive=True):
        try:
            for line in open(p, errors="ignore"):
                line = line.strip()
                if not line:
                    continue
                try:
                    o = json.loads(line)
                except Exception:
                    continue
                m = o.get("message") or {}
                u = m.get("usage")
                model = m.get("model")
                if not (u and model):
                    continue
                mid = m.get("id")
                c = cost(u, model)
                if mid:
                    if mid in seen:
                        continue
                    seen.add(mid)
                    mid_val += c
                else:
                    nomid_val += c
                    nomid_n += 1
                    nomid_models[model] = nomid_models.get(model, 0) + 1
        except Exception:
            continue
    print("mid-bearing deduped est-value:  $%.2f" % mid_val)
    print("mid-LESS usage turns:           %d turns, $%.2f" % (nomid_n, nomid_val))
    print("mid-less by model:", dict(sorted(nomid_models.items(), key=lambda x: -x[1])[:6]))


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Extract the OBSERVABLE quota/limit signals from Claude Code transcripts: the `quotaLimits` records (the plan's
own reported caps) and the API rate/limit ERROR records (when the cap was actually hit). These are what let
spendguard CALCULATE billing_state from observable data — the cap and the cap-hit moments come from the transcript
itself, not from the provider admin API (which stays a dev cross-check). Read-only, $0."""
import os
import glob
import json
from collections import Counter

P = os.path.expanduser(os.environ.get("SPENDGUARD_CC_DIR", "~/.claude/projects"))


def main():
    files = glob.glob(os.path.join(P, "**", "*.jsonl"), recursive=True)
    quota_records = []
    err_status = Counter()
    err_samples = []
    stop_reasons = Counter()
    quota_limit_texts = Counter()
    for f in files:
        try:
            for line in open(f, errors="ignore"):
                line = line.strip()
                if not line:
                    continue
                if ("quotaLimits" not in line and "isApiErrorMessage" not in line
                        and "apiErrorStatus" not in line and '"quotaLimit' not in line):
                    # cheap pre-filter; still catch stop_reason below only on parsed error lines
                    if '"stop_reason"' not in line:
                        continue
                try:
                    o = json.loads(line)
                except Exception:
                    continue
                if o.get("quotaLimits") is not None and len(quota_records) < 8:
                    quota_records.append({"ts": o.get("timestamp"), "quotaLimits": o.get("quotaLimits")})
                if o.get("isApiErrorMessage") or o.get("apiErrorStatus"):
                    st = str(o.get("apiErrorStatus"))
                    err_status[st] += 1
                    if len(err_samples) < 6:
                        m = o.get("message") or {}
                        txt = m.get("content") if isinstance(m.get("content"), str) else json.dumps(m.get("content"))[:300]
                        err_samples.append({"ts": o.get("timestamp"), "status": st, "text": (txt or "")[:300]})
                m = o.get("message") or {}
                sr = m.get("stop_reason")
                if sr:
                    stop_reasons[str(sr)] += 1
        except Exception:
            continue
    print("=== quotaLimits records (the plan's OWN reported caps) ===")
    for q in quota_records:
        print(" ", q["ts"], json.dumps(q["quotaLimits"])[:600])
    if not quota_records:
        print("  (none found)")
    print("\n=== API error status distribution (429 = rate/quota limit) ===")
    print(" ", dict(err_status.most_common()))
    print("\n=== sample error messages ===")
    for e in err_samples:
        print(" ", e["ts"], "·", e["status"], "·", e["text"])
    print("\n=== stop_reason distribution ===")
    print(" ", dict(stop_reasons.most_common(12)))


if __name__ == "__main__":
    main()

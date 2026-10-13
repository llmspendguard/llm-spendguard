"""Guard: claudecode._overage_events is INCREMENTALLY cached by file (mtime,size) and the cached result is identical
to a full scan.

Why it matters: _overage_events re-reads the whole claude-code transcript tree (~10s) and sits on plan_admission's
hot path via overage.current_overage_status. The per-file cache must (a) produce the SAME events as an uncached full
scan (full-history callers reconcile_billing_state / attribute_overage depend on it), (b) persist a per-file cache
keyed by a (mtime,size) signature, and (c) INVALIDATE a file whose content grew (new events appear) — never serve a
stale parse for changed content.

End-to-end + offline: writes real .jsonl transcripts into a temp projects dir (SPENDGUARD_CC_DIR) and drives the real
function. No monkeypatching of internals, no network, no LLM. SPENDGUARD_TEST_ISOLATED is deliberately NOT set, so the
cache path is exercised (it is bypassed under isolation).
"""
import json
import os
import sys
import tempfile
import time

_HOME = tempfile.mkdtemp(prefix="sg-ovg-inc-home-")
_CC = tempfile.mkdtemp(prefix="sg-ovg-inc-cc-")
os.environ["SPENDGUARD_HOME"] = _HOME
os.environ["SPENDGUARD_CC_DIR"] = _CC
os.environ.setdefault("SPENDGUARD_NO_AUTOINSTALL", "1")
os.environ.pop("SPENDGUARD_TEST_ISOLATED", None)             # exercise the cache (it is bypassed under isolation)
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from spendguard import claudecode  # noqa: E402

fails = []


def ck(label, condition, detail=""):
    print(f"  [{'OK' if condition else 'FAIL'}] {label}" + (f" — {detail}" if detail and not condition else ""))
    if not condition:
        fails.append(label)


def _seven_day_line(ts, resets_at):
    return json.dumps({"timestamp": ts, "quotaLimits": {"rateLimitType": "seven_day", "resetsAt": resets_at}})


def _write(name, lines):
    p = os.path.join(_CC, name)
    with open(p, "w") as fh:
        fh.write("\n".join(lines) + "\n")
    return p


# Two transcripts; only one carries a weekly cap-hit.
_write("conv_a.jsonl", [json.dumps({"timestamp": "2026-10-01T00:00:00Z", "message": {"content": "hi"}}),
                        _seven_day_line("2026-10-02T00:00:00Z", 1800000000.0)])
pb = _write("conv_b.jsonl", [json.dumps({"timestamp": "2026-10-03T00:00:00Z", "message": {"content": "no limits here"}})])

# Ground truth: a full scan under isolation (cache bypassed).
os.environ["SPENDGUARD_TEST_ISOLATED"] = "1"
full = claudecode._overage_events()
os.environ.pop("SPENDGUARD_TEST_ISOLATED", None)


def _norm(d):
    return {k: (sorted(v) if k == "weekly_resets" else v) for k, v in d.items()}


# Cold cached scan builds the cache; warm reuses it — both must equal the full scan.
cold = claudecode._overage_events()
warm = claudecode._overage_events()
ck("cold cached scan == full scan", _norm(cold) == _norm(full), (cold, full))
ck("warm cached scan == full scan", _norm(warm) == _norm(full), (warm, full))
ck("the one weekly cap-hit was found", len(full["weekly_hits"]) == 1 and len(full["weekly_resets"]) == 1, full)

cache_path = claudecode._overage_events_cache_path()
ck("a per-file cache file was written", os.path.exists(cache_path), cache_path)
blob = json.load(open(cache_path)) if os.path.exists(cache_path) else {}
ck("the cache holds a per-file entry with a (mtime,size) sig for each transcript",
   all(isinstance(v, dict) and ":" in str(v.get("sig", "")) for v in blob.values()) and len(blob) == 2, blob)
sig_b_before = (blob.get(pb) or {}).get("sig")

# Append a NEW weekly cap-hit to conv_b → its (mtime,size) changes → the cache must INVALIDATE and pick it up.
time.sleep(0.01)
with open(pb, "a") as fh:
    fh.write(_seven_day_line("2026-10-04T00:00:00Z", 1800600000.0) + "\n")
after = claudecode._overage_events()
ck("a grown transcript is re-read — the new cap-hit appears (no stale serve)",
   len(after["weekly_hits"]) == len(full["weekly_hits"]) + 1, after)
blob2 = json.load(open(cache_path))
ck("the changed file's cache sig was updated", (blob2.get(pb) or {}).get("sig") != sig_b_before,
   (sig_b_before, (blob2.get(pb) or {}).get("sig")))

print(f"\n{'[FAIL]' if fails else 'OK'} test_overage_events_incremental_cache: {len(fails)} failure(s)")
sys.exit(1 if fails else 0)

"""Batch-C unguarded-path closure (line-by-line medium fixes). The ones with a clean, offline-testable contract:

  * share import: a non-numeric confidence in an UNTRUSTED file → default 0.4 (no crash); an EXPLICIT 0 stays 0
    (not silently upgraded by a falsy `or`).
  * submit gate: two same-named .jsonl files in DIFFERENT dirs get DISTINCT audit records (was: one overwrote
    the other via basename-only keying).
  * trust.provider_truth: date-OBJECT day keys vs a string `since` no longer raise a TypeError that masqueraded
    as a fetch failure (str-vs-str compare).

(The other Batch-C fixes — realtime_oracle / lanes / runpod / prompts / advisor / sync / workdone guards — are
mechanical try/except+close and are covered by their modules' suites staying green.)

Offline, isolated home.
"""
import os
import sys
import tempfile
import json
import datetime

if not os.environ.get("SPENDGUARD_TEST_ISOLATED"):
    os.environ["SPENDGUARD_TEST_ISOLATED"] = "1"
    os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="spendguard-medC-")
    os.execv(sys.executable, [sys.executable] + sys.argv)

from spendguard import share, submit, trust      # noqa: E402

fails = 0


def ck(label, cond, extra=""):
    global fails
    if not cond:
        fails += 1
    print(f"  [{'OK' if cond else 'FAIL'}] {label}{('  — ' + extra) if extra and not cond else ''}")


# ── share import: confidence parsing ─────────────────────────────────────────────────────────────────────────
import spendguard.learn as _learn        # noqa: E402
captured = []
_learn.add_insight = lambda *a, **k: captured.append(k.get("confidence"))
fp = os.path.join(tempfile.mkdtemp(), "insights.json")
with open(fp, "w") as f:
    json.dump({"insights": [{"lesson": "L1", "confidence": "high"},     # non-numeric → default, no crash
                            {"lesson": "L2", "confidence": 0}]}, f)      # explicit 0 → stays 0
crashed = False
try:
    share.cmd_import([fp, "--trust", "0.9"])
except Exception:
    crashed = True
ck("import does not crash on a non-numeric confidence", not crashed)
ck("non-numeric confidence falls back to the 0.4 default",
   len(captured) == 2 and abs((captured[0] or -1) - 0.4) < 1e-9, f"got {captured}")
ck("an explicit confidence of 0 is NOT silently upgraded to 0.4", captured[1] == 0.0, f"got {captured}")

# ── submit gate: same-named files in different dirs get distinct audit records ─────────────────────────────────
line = json.dumps({"custom_id": "a", "body": {"model": "gpt-5.5",
                                              "messages": [{"role": "user", "content": "hi"}], "max_tokens": 10}}) + "\n"
for d in (tempfile.mkdtemp(), tempfile.mkdtemp()):
    p = os.path.join(d, "batch.jsonl")
    with open(p, "w") as f:
        f.write(line)
    submit.guarded_submit(p, "gpt-5.5", None, submit=False)
gate_files = [a for a in os.listdir(submit.AUDIT_DIR) if a.endswith(".gate.json")]
ck("two same-named .jsonl in different dirs produce TWO distinct audit records", len(gate_files) == 2, f"got {gate_files}")

# ── trust.provider_truth: date-object keys don't crash ─────────────────────────────────────────────────────────
import spendguard.report as _report          # noqa: E402
import spendguard.reconcile_anthropic as _ra  # noqa: E402
import spendguard.gate as _gate               # noqa: E402
_report.openai_by_day = lambda: ({datetime.date(2026, 8, 10): 5.0}, 0)     # DATE-OBJECT keys
_ra.cost_by_day = lambda since=None: ({datetime.date(2026, 8, 11): 3.0}, {})
_gate.realtime_by_day = lambda since=None: ({}, {})
t = trust.provider_truth(since="2026-08-01")
ck("provider_truth tolerates date-object keys (no TypeError→UNKNOWN masquerade)", t == 8.0, f"got {t}")

print(f"\n{'[FAIL]' if fails else 'OK'} test_medium_closure_batchC: {fails} failure(s)")
sys.exit(1 if fails else 0)

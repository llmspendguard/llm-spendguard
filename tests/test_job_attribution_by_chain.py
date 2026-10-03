"""#1 JOB ATTRIBUTION on the money ledger. A billed charge now carries the caller's JOB/RUN tag — `chain`, resolved
from `spendguard.context(chain=…)` — onto spend_events, so `budget.spent_by_job(job)` isolates ONE run even when a
CONCURRENT run shares the intent. That concurrency collision is exactly what made an intent+since read over-count
(two jobs of one intent summed together). Here two same-intent runs with distinct chains stay separable.

Offline, isolated SPENDGUARD_HOME, zero spend (direct charge records; no LLM)."""
import os
import sys
import tempfile

os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="sg-jobattr-")
os.environ.setdefault("SPENDGUARD_TEST_ISOLATED", "1")
os.environ.setdefault("SPENDGUARD_NO_AUTOINSTALL", "1")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from spendguard import budget, calls  # noqa: E402

fails = []


def ck(name, cond, extra=""):
    print(("  [OK] " if cond else "  [FAIL] ") + name + (("  — " + extra) if extra and not cond else ""))
    if not cond:
        fails.append(name)


INTENT = "shared-intent"
# Two runs of the SAME intent, distinct job tags — the chain is RESOLVED FROM CONTEXT (record_charge reads it from
# spendguard.context), never passed; this is how a real gated call is tagged.
with calls.context(intent=INTENT, chain="job-A"):
    budget.record_charge("openai", "gpt-5-mini", "realtime", 0.03)
    budget.record_charge("openai", "gpt-5-mini", "realtime", 0.02)     # job-A total = 0.05
with calls.context(intent=INTENT, chain="job-B"):
    budget.record_charge("openai", "gpt-5-mini", "realtime", 0.10)     # job-B total = 0.10

a = budget.spent_by_job("job-A")
b = budget.spent_by_job("job-B")
ck("spent_by_job('job-A') = 0.05 — this run ONLY, not contaminated by the concurrent job-B", abs(a - 0.05) < 1e-9,
   extra=f"a={a}")
ck("spent_by_job('job-B') = 0.10 — isolated", abs(b - 0.10) < 1e-9, extra=f"b={b}")
ck("the jobs do NOT collide: job-A (0.05) != the combined 0.15 an intent-wide read would return",
   abs(a - 0.05) < 1e-9 and abs((a + b) - 0.15) < 1e-9, extra=f"a={a} a+b={a + b}")
ck("an empty / unknown job is 0.0 (never a false total)", budget.spent_by_job("") == 0.0 and budget.spent_by_job("nope") == 0.0)

# the money ROW carries chain (resolved from context, written through charge_to_event → spend_events), so it is
# queryable later / cross-process — not just an in-process flow delta.
led = budget._ledger()
chains = sorted({r.get("chain") for r in led.query() if r.get("chain")})
ck("the money ledger rows carry the chain job tag ({'job-A','job-B'})", chains == ["job-A", "job-B"], extra=repr(chains))

print(f"\n{'OK' if not fails else 'FAIL'} test_job_attribution_by_chain: {len(fails)} failure(s)")
sys.exit(1 if fails else 0)

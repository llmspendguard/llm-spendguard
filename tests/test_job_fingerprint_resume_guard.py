"""job_fingerprint stops a resume from silently re-buying (plan drift) or reusing stale answers (method drift) —
the resumable-job guard (caller-feedback #7).

Locks: the three components hash SEPARATELY; compare() flags plan vs method drift; guard_resume returns fresh/match,
RAISES JobPlanDrift on plan drift (and honors allow_replan), adjudicates method drift via a handler (reuse/revalidate/
rebuy) and RAISES JobMethodUndecided with no handler, and FAILS CLOSED on a file that has rows but no fingerprint. The
refusals are SpendGateRefused subclasses, so they propagate through the gate's deliberate-stop machinery (never sys.exit).

Offline, isolated SPENDGUARD_HOME, zero spend (hashing + file I/O).
"""
import os
import sys
import json
import tempfile

if not os.environ.get("SPENDGUARD_TEST_ISOLATED"):
    os.environ["SPENDGUARD_TEST_ISOLATED"] = "1"
    os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="sg-jobfp-")
    os.execv(sys.executable, [sys.executable] + sys.argv)

from spendguard import job_fingerprint as jf  # noqa: E402
from spendguard.gate import SpendGateRefused  # noqa: E402

HOME = os.environ["SPENDGUARD_HOME"]


class Checks:
    def __init__(self):
        self.fails = []

    def __call__(self, label, cond, extra=""):
        if not cond:
            self.fails.append(label)
        print(f"  [{'OK' if cond else 'FAIL'}] {label}{('  — ' + extra) if extra and not cond else ''}")


ck = Checks()


def seed(name, fingerprint=None, n_rows=3):
    path = os.path.join(HOME, name)
    with open(path, "w", encoding="utf-8") as f:
        if fingerprint is not None:
            f.write(json.dumps({jf.FINGERPRINT_KEY: fingerprint, "detail": {}}) + "\n")
        for i in range(n_rows):
            f.write(json.dumps({"work_key": f"k{i}", "result": "x"}) + "\n")
    return path


BASE = jf.job_fingerprint("task-a", 10, ["i1", "i2", "i3"], prompt_sample="PROMPT V1", model="gpt-5.5")

# ── the three components hash SEPARATELY; a None component is omitted ───────────────────────────────────────────────
ck("fingerprint carries plan/prompt/model separately", set(BASE) == {jf.PLAN, jf.PROMPT, jf.MODEL}, extra=repr(BASE))
plan_only = jf.job_fingerprint("task-a", 10, ["i1", "i2", "i3"])
ck("a None prompt/model is OMITTED (plan-only marker)", set(plan_only) == {jf.PLAN})
ck("the plan hash is stable across runs that differ only in prompt/model", plan_only[jf.PLAN] == BASE[jf.PLAN])

# ── compare() flags plan vs method drift ───────────────────────────────────────────────────────────────────────────
drift_plan = jf.job_fingerprint("task-a", 8, ["i1", "i2", "i3"], prompt_sample="PROMPT V1", model="gpt-5.5")   # pack 10→8
pc, mc = jf.compare_fingerprints(BASE, drift_plan)
ck("a pack-size change is PLAN drift", pc is True and mc == [], extra=f"pc={pc} mc={mc}")
drift_prompt = jf.job_fingerprint("task-a", 10, ["i1", "i2", "i3"], prompt_sample="PROMPT V2", model="gpt-5.5")
pc2, mc2 = jf.compare_fingerprints(BASE, drift_prompt)
ck("a prompt change is METHOD drift (plan unchanged)", pc2 is False and mc2 == [jf.PROMPT], extra=f"pc={pc2} mc={mc2}")

# ── guard_resume: fresh → match ────────────────────────────────────────────────────────────────────────────────────
p_fresh = os.path.join(HOME, "fresh.jsonl")
ck("an empty/new file is 'fresh' (writes the marker)", jf.guard_resume(p_fresh, BASE, {"note": "run1"}) == "fresh")
# after fresh, the same fingerprint on the now-rows... fresh.jsonl has only the marker (no work rows) → still "fresh"
# so seed a file WITH rows + the marker to get a match:
p_match = seed("match.jsonl", fingerprint=BASE)
ck("an identical fingerprint over recorded rows is 'match'", jf.guard_resume(p_match, BASE, {}) == "match")

# ── plan drift RAISES JobPlanDrift (a SpendGateRefused) ────────────────────────────────────────────────────────────
p_plan = seed("plan.jsonl", fingerprint=BASE)
try:
    jf.guard_resume(p_plan, drift_plan, {"pack": 8})
    ck("plan drift raises JobPlanDrift", False, "did not raise")
except jf.JobPlanDrift as e:
    ck("plan drift raises JobPlanDrift", True)
    ck("JobPlanDrift is a SpendGateRefused (propagates via the gate)", isinstance(e, SpendGateRefused))

# ── plan drift + allow_replan → 'rebuy' (deliberate), no raise ─────────────────────────────────────────────────────
p_replan = seed("replan.jsonl", fingerprint=BASE)
ck("allow_replan turns plan drift into a deliberate 'rebuy'",
   jf.guard_resume(p_replan, drift_plan, {"pack": 8}, allow_replan=True) == "rebuy")

# ── method drift: no handler RAISES JobMethodUndecided; with a handler it adjudicates ───────────────────────────────
p_m1 = seed("method_undecided.jsonl", fingerprint=BASE)
try:
    jf.guard_resume(p_m1, drift_prompt, {})
    ck("method drift with no handler raises JobMethodUndecided", False, "did not raise")
except jf.JobMethodUndecided as e:
    ck("method drift with no handler raises JobMethodUndecided", True)
    ck("JobMethodUndecided is a SpendGateRefused", isinstance(e, SpendGateRefused))

p_reuse = seed("method_reuse.jsonl", fingerprint=BASE)
ck("handler → 'reuse' is honored", jf.guard_resume(p_reuse, drift_prompt, {}, on_method_change=lambda *_a: "reuse") == "reuse")
p_reval = seed("method_reval.jsonl", fingerprint=BASE)
ck("handler → 'revalidate' maps to 'revalidated'", jf.guard_resume(p_reval, drift_prompt, {}, on_method_change=lambda *_a: "revalidate") == "revalidated")
p_rebuy = seed("method_rebuy.jsonl", fingerprint=BASE)
ck("handler → 'rebuy' is honored", jf.guard_resume(p_rebuy, drift_prompt, {}, on_method_change=lambda *_a: "rebuy") == "rebuy")

# ── FAIL CLOSED: rows but NO fingerprint marker → treated as plan drift (cannot vouch) ──────────────────────────────
p_nofp = seed("nofp.jsonl", fingerprint=None)      # work rows, no marker (written before the guard existed)
try:
    jf.guard_resume(p_nofp, BASE, {})
    ck("a file with rows but no fingerprint FAILS CLOSED (raises)", False, "did not raise")
except jf.JobPlanDrift:
    ck("a file with rows but no fingerprint FAILS CLOSED (raises)", True)

print(f"\n{'OK' if not ck.fails else 'FAIL'} test_job_fingerprint_resume_guard: {len(ck.fails)} failure(s)")
sys.exit(1 if ck.fails else 0)

"""Staged validation of the lifecycle EVAL gate, END-TO-END with a REAL agentic judge.

Shows the full transition the design promises:
  STAGE 1  a >=$0.25 multi-unit scale run with NOTHING recorded → BLOCKED
  STAGE 2  after estimate + a shape-verified test (but no eval)   → STILL BLOCKED (shape != quality)
  STAGE 3  eval_job runs a REAL cheap judge on a GOOD sample vs a STATED bar → PASS → scale AUTHORIZED
  STAGE 4  the SAME bar on a BAD sample → the judge FAILS it → scale STAYS BLOCKED
STAGE 4 is the important one: a different verdict for a bad sample proves the judge is AGENTIC, not a rubber-stamp.

Run under the gate: `.venv.nosync/bin/python scripts/lifecycle/demo_eval_gate.py`. One cheap judge call per eval,
caged as the meta intent `spendguard:eval`. Set SPENDGUARD_EVAL_MODEL to pick the judge (else advisor_judge_model).
"""
import os
import sys
import tempfile

os.environ.setdefault("SPENDGUARD_HOME", tempfile.mkdtemp(prefix="sg-evaldemo-"))
os.environ["SPENDGUARD_ENFORCE"] = "block"                 # the hard gate, so BLOCKED is real, not shadow
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "src"))

import spendguard                                                                      # noqa: E402,F401
from spendguard import bulkgate                                                        # noqa: E402

MODEL = "gpt-5.5"
SIG = bulkgate.sig(MODEL, template_id="patient-note-extract")
BAR = ("each output is a JSON object with a NON-EMPTY 'findings' string and a 'patient_id' that looks like a real "
       "identifier (not '0', not empty)")
COUNT, EST = 2000, 4.20


def show(sig, label):
    st = bulkgate.status(sig)
    print(f"    status: fresh={st['fresh']}  eval_ok={st.get('eval_ok')}  reason={st.get('reason')!r}")


print("STAGE 1 — $%.2f / %d-unit scale run, NOTHING recorded:" % (EST, COUNT))
try:
    bulkgate.check_bulk(SIG, MODEL, COUNT, EST)
    print("    UNEXPECTED: allowed")
except bulkgate.GateBlocked as e:
    print("    BLOCKED ✓ —", str(e)[:110], "...")

print("\nSTAGE 2 — record estimate + a shape-verified test (still no eval):")
bulkgate.record_estimate(SIG, MODEL, EST, COUNT)
bulkgate.record_tested(SIG, 5, verified=True)
show(SIG, "after estimate+test")
try:
    bulkgate.check_bulk(SIG, MODEL, COUNT, EST)
    print("    UNEXPECTED: allowed")
except bulkgate.GateBlocked:
    print("    still BLOCKED ✓ (shape passed, but quality not yet judged)")

print("\nSTAGE 3 — eval_job: a REAL judge scores a GOOD sample against the stated bar:")
good_sample = [
    '{"patient_id": "PT-40912", "findings": "mild cardiomegaly; no acute infiltrate"}',
    '{"patient_id": "PT-40913", "findings": "small left pleural effusion, stable"}',
    '{"patient_id": "PT-40914", "findings": "no acute cardiopulmonary abnormality"}',
]
v = bulkgate.eval_job(SIG, bar=BAR, sample=good_sample)
print(f"    judge: pass={v['pass']} score={v['score']} — {v['rationale'][:110]}")
show(SIG, "after a passing eval")
print("    check_bulk →", bulkgate.check_bulk(SIG, MODEL, COUNT, EST), "✓")

print("\nSTAGE 4 — the SAME bar on a BAD sample must FAIL (proves the judge is real, not a rubber-stamp):")
SIG_BAD = bulkgate.sig(MODEL, template_id="patient-note-extract-bad")
bulkgate.record_estimate(SIG_BAD, MODEL, EST, COUNT)
bulkgate.record_tested(SIG_BAD, 5, verified=True)
bad_sample = ['{"patient_id": "0", "findings": ""}', '{"patient_id": "", "findings": "n/a"}',
              '{"patient_id": "0", "findings": ""}']
vb = bulkgate.eval_job(SIG_BAD, bar=BAR, sample=bad_sample)
print(f"    judge: pass={vb['pass']} score={vb['score']} — {vb['rationale'][:100]}")
try:
    bulkgate.check_bulk(SIG_BAD, MODEL, COUNT, EST)
    print("    UNEXPECTED: allowed a failing-eval sig")
except bulkgate.GateBlocked:
    print("    BLOCKED ✓ — a failing eval keeps scale gated until a passing one exists")

print("\n(one cheap judge call per eval, caged as spendguard:eval)")

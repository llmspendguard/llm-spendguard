"""The batch OUTPUT-TOKEN estimation basis (the fix for: the estimate used the model ceiling × request count — ~100x
over — labelled the lying 'caller-cap', which made a $ cap unusable for authorization and trained fail-open). What we
SEND stays the ceiling; what we ESTIMATE is what the job will PLAUSIBLY emit: MEASURED (per-intent) → DECLARED → ceiling,
each named honestly. Offline (no network, no spend): estimates are pure token counting + the bulkgate DB; isolated HOME.

Covers the spec's seven cases:
  1. an intent with recorded history estimates from the MEASURED basis, and out_basis says so;
  2. a cold intent (no history, no declaration) falls back to the ceiling and SAYS 'ceiling' (not the 'caller-cap' lie);
  3. a caller-declared expected_out_tokens is used and labelled 'declared';
  4. the SEPARATE ceiling guard still refuses a worst-case-exceeding request set even when the realistic estimate is small;
  5. a refusal names BOTH the caller cap and the global cap and which one bound;
  6. the provisional basis is the REALISTIC estimate, not the ceiling — so an uncollected/expired batch can't leave a
     ~100x overstatement in the ledger (the $-truth is then reconciled to provider billing);
  7. max_out is not silently dropped — a passed max_out is pointed at expected_out_tokens (how to declare).
"""
import io
import os
import sys
import tempfile
import contextlib

os.environ["SPENDGUARD_HOME"] = os.environ.get("SPENDGUARD_HOME") or tempfile.mkdtemp(prefix="sg-batch-est-")
os.environ["SPENDGUARD_TEST_ISOLATED"] = "1"
os.environ.setdefault("SPENDGUARD_NO_AUTOINSTALL", "1")
os.environ["ANTHROPIC_API_KEY"] = "sk-ant-test-FAKE"
os.environ.pop("GATE_ALLOW", None)                 # the worst-case guard must actually fire (GATE_ALLOW forces past it)
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from spendguard import submit, gate, bulkgate      # noqa: E402

MODEL = "claude-haiku-4-5"          # seeded with history in case 1 — so model-history fires for ANY intent on it
COLD_MODEL = "claude-sonnet-4-5"    # never seeded here → genuinely cold (no learned, no model-history) for the ceiling cases
TASKS = [{"custom_id": str(i), "content": "classify this clinical symptom surface form"} for i in range(22)]


def _estimate(intent=None, declared=None, model=MODEL):
    reqs, _ = submit.build_message_batch_requests(TASKS, model)
    return gate.estimate_message_batch(reqs, intent=intent, declared_out=declared)


def main():
    fails = []

    def ck(name, cond, extra=""):
        print(("  [OK] " if cond else "  [FAIL] ") + name + (("  — " + str(extra)) if extra and not cond else ""))
        if not cond:
            fails.append(name)

    # 1) MEASURED basis — seed ≥ MIN_OBS complete outputs for this intent's call-class, then the estimate uses them.
    intent = "test:symptom-surface"
    sig = bulkgate.sig(MODEL, template_id=intent)
    for _ in range(25):
        bulkgate.note_response(sig, MODEL, 900, finish_reason="end_turn")   # complete (not truncated) → counts toward p90
    seeded = bulkgate.maxtokens(sig)
    ck("bulkgate seeded ≥20 complete outputs for the intent", (seeded.get("n") or 0) >= 20, seeded.get("n"))
    em = _estimate(intent=intent)
    ck("measured intent → out_basis 'measured:<intent>'", em["out_basis"] == "measured:" + intent, em["out_basis"])
    ck("measured out_tok ≈ p90 × requests (not the 128k ceiling)", 0 < em["out_tok"] < 50_000, em["out_tok"])

    # 2) COLD intent on a COLD model (no history, no declaration) → the ceiling, HONESTLY labelled (not 'caller-cap')
    ec = _estimate(intent="test:never-seen-cold", model=COLD_MODEL)
    ck("cold intent → out_basis 'ceiling' (honest, not 'caller-cap')", ec["out_basis"] == "ceiling", ec["out_basis"])
    ck("cold out_tok = the ceiling × requests", ec["out_tok"] == ec["ceiling_out"], (ec["out_tok"], ec["ceiling_out"]))

    # 3) DECLARED expected_out_tokens → used + labelled 'declared' (beats even a broad model-history)
    ed = _estimate(intent="test:never-seen-cold", declared=1200, model=COLD_MODEL)
    ck("declared → out_basis 'declared'", ed["out_basis"] == "declared", ed["out_basis"])
    ck("declared out_tok = declared × requests (1200 × 22)", ed["out_tok"] == 1200 * len(TASKS), ed["out_tok"])
    ck("declared cost ≪ the ceiling worst case", ed["cost"] < ed["worst_case_cost"], (ed["cost"], ed["worst_case_cost"]))

    # 4) the SEPARATE ceiling guard refuses a runaway worst-case even when the realistic estimate is small
    est_small = {"provider": "anthropic", "model": MODEL, "requests": 10, "cost": 0.50, "out_basis": "declared",
                 "worst_case_cost": 100.0}
    _prev = os.environ.get("SPENDGUARD_WORST_CASE_CAP")
    os.environ["SPENDGUARD_WORST_CASE_CAP"] = "1.0"
    try:
        refused = False
        try:
            gate._worst_case_check(est_small)
        except gate.SpendGateRefused:
            refused = True
        ck("worst-case guard REFUSES (worst_case $100 > cap $1) despite the realistic estimate being $0.50", refused)
        os.environ["SPENDGUARD_WORST_CASE_CAP"] = "1000.0"
        ok = True
        try:
            gate._worst_case_check(est_small)         # $100 < $1000 → must NOT refuse
        except gate.SpendGateRefused:
            ok = False
        ck("worst-case guard allows when the worst case is under its (separate, higher) cap", ok)
        ck("worst-case guard is a no-op when no worst_case_cost was carried",
           (gate._worst_case_check({"provider": "anthropic", "model": MODEL, "cost": 0.5}) or True))
    finally:
        if _prev is None:
            os.environ.pop("SPENDGUARD_WORST_CASE_CAP", None)
        else:
            os.environ["SPENDGUARD_WORST_CASE_CAP"] = _prev

    # 5) a refusal NAMES BOTH caps + which one bound
    r = submit.submit_message_batch(TASKS, MODEL, intent="test:never-seen-cold", expected_out_tokens=1200,
                                    cap_dollars=0.01, submit=False)
    err = r.get("error") or ""
    ck("refusal names the caller cap", "caller cap" in err, err)
    ck("refusal names the global GATE_CAP", "GATE_CAP" in err, err)
    ck("refusal states which one bound (caller cap binds here)", "> caller cap" in err, err)

    # 6) the PROVISIONAL basis is the realistic estimate, not the ceiling → no ~100x can sit in the ledger uncollected.
    # The provisional row is booked at the gate from est['cost'] (the realistic basis); est['worst_case_cost'] is the
    # ceiling, kept only for the separate guard. ($-truth is then reconciled to provider billing; collect records the
    # REAL usage to the corpus — see test_message_batch_collect.)
    prov = _estimate(intent=intent)                   # the measured intent from case 1
    ck("provisional (est['cost']) is the REALISTIC basis, not the ceiling", prov["cost"] < prov["worst_case_cost"],
       (prov["cost"], prov["worst_case_cost"]))
    ck("provisional out_basis is a measurement, never the lying 'caller-cap'", prov["out_basis"].startswith("measured:"), prov["out_basis"])

    # 7) max_out is not silently dropped — the caller is pointed at expected_out_tokens
    buf = io.StringIO()
    with contextlib.redirect_stderr(buf):
        submit.submit_message_batch(TASKS, MODEL, intent="test:never-seen-cold", max_out=8000, submit=False)
    warned = buf.getvalue()
    ck("a passed max_out prints how to declare (expected_out_tokens)", "expected_out_tokens" in warned, warned[:120])

    print(f"\n{'[FAIL]' if fails else 'OK'} test_batch_estimate_basis: {len(fails)} failure(s)")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())

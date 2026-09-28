"""Guard for the budget/spend-gate refusal MISCLASSIFICATION fix (2026-09-27): a SpendGateRefused (a deliberate
budget/meta/RT/batch-1 cap) must be classified GATE_REFUSED — a NON-retryable, deliberate outcome whose .text raises —
never talked into transport_error and retried against the very cap that refused it (which the class-aware queue
retry-to-10 would otherwise do, wasting attempts on a deliberate stop and violating the never-fail-open doctrine).

The mechanism: vendor_call._attempt marks a caught deliberate stop (`deliberate_stop`, decided by the canonical
gate.is_deliberate_stop authority), and vendor_call._classify maps a marked result to GATE_REFUSED FIRST — before the
error→status→transport branch. Offline + deterministic ($0, no provider call).

Isolation: SPENDGUARD_HOME → tempfile.mkdtemp before importing spendguard (fresh subprocess per test in chunked_suite).
"""
import os, sys, tempfile

os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="spendguard-gaterefuse-")

from spendguard import vendor_call as vc, gate   # noqa: E402


class Checks:
    def __init__(self):
        self.fails = 0

    def ck(self, label, cond, extra=""):
        if not cond:
            self.fails += 1
        print(f"  [{'OK' if cond else 'FAIL'}] {label}{('  — ' + extra) if extra and not cond else ''}")


def main():
    c = Checks()

    # taxonomy: GATE_REFUSED is a first-class kind and is NOT retryable
    c.ck("GATE_REFUSED is in KINDS", vc.GATE_REFUSED in vc.KINDS)
    c.ck("GATE_REFUSED is NOT in RETRYABLE (never retried)", vc.GATE_REFUSED not in vc.RETRYABLE)

    # the canonical authority recognises a SpendGateRefused as a deliberate stop
    c.ck("gate.is_deliberate_stop(SpendGateRefused) is True",
         gate.is_deliberate_stop(gate.SpendGateRefused("total-daily budget $500 would be exceeded")) is True)

    # a MARKED (deliberate) result classifies as GATE_REFUSED — not transport_error
    marked = {"error": "SpendGateRefused: total-daily budget $500 would be exceeded (projected $501). GATE_ALLOW=1",
              "text": None, "deliberate_stop": True, "error_type": "SpendGateRefused"}
    k, _ = vc._classify(marked)
    c.ck("marked budget refusal → GATE_REFUSED (not transport_error)", k == vc.GATE_REFUSED, "got %s" % k)

    # REGRESSION GUARD: a genuine transport error (no marker, no status) still classifies transport_error — the fix
    # must not over-classify ordinary faults as refusals.
    k2, _ = vc._classify({"error": "connection reset by peer", "text": None})
    c.ck("unmarked transport error → transport_error (unchanged)", k2 == vc.TRANSPORT_ERROR, "got %s" % k2)

    # a real 429 still classifies overloaded (retryable) — the fix is narrow to the deliberate-stop marker
    k3, _ = vc._classify({"error": "rate limited", "text": None, "status_code": 429})
    c.ck("429 → overloaded (still retryable)", k3 == vc.OVERLOADED and vc.OVERLOADED in vc.RETRYABLE, "got %s" % k3)

    # Result.text RAISES on GATE_REFUSED (non-ok) — the refusal can never be read as a short/empty success
    r = vc.Result(vc.GATE_REFUSED, "openai", "gpt-5-nano", error="budget refused")
    c.ck("Result(GATE_REFUSED).ok is False", r.ok is False)
    raised = False
    try:
        _ = r.text
    except Exception:
        raised = True
    c.ck("Result(GATE_REFUSED).text raises (never a silent empty)", raised)

    print(f"\n{'[FAIL]' if c.fails else 'OK'} test_gate_refusal_not_retryable: {c.fails} failure(s)")
    return 1 if c.fails else 0


if __name__ == "__main__":
    sys.exit(main())

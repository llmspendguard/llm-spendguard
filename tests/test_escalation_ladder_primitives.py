"""Offline checks for the honestreview escalation-ladder primitives."""
import os
import sys
import tempfile
import threading
import time

os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="spendguard-ladder-")

from spendguard import calls  # noqa: E402
from spendguard.escalation_ladder import cancel_on_zero_billable, relative_escalation_gate  # noqa: E402


class Checks:
    def __init__(self):
        self.fails = 0

    def ck(self, label, condition, extra=""):
        if not condition:
            self.fails += 1
        print(f"  [{'OK' if condition else 'FAIL'}] {label}{(' — ' + extra) if extra and not condition else ''}")


def main():
    c = Checks()
    stopped = threading.Event()

    def stalled():
        stopped.wait(1)
        return "late"

    cancelled = cancel_on_zero_billable(
        stalled, 0.01, transport="subscription",
        cancel=lambda: not stopped.is_set() and (stopped.set() or True),
        provider="openai", model="gpt-test", intent="test:stall",
    )
    c.ck("zero-token subscription stall cancels", cancelled["stall_outcome"] == "stalled_zerobill_cancelled")
    c.ck("cancel bills nothing", cancelled["cost"] == 0.0)
    row = calls._calls_db().execute(
        "SELECT disposition, disposition_reason, cost, quality, finish FROM calls ORDER BY ts DESC LIMIT 1"
    ).fetchone()
    c.ck("distinct cancel disposition recorded", row and row[0] == "cancelled-stall-zerobill", str(row))
    c.ck("cancel reason recorded", row and "zero billable" in row[1], str(row))
    c.ck("cancel is not quality-bad", row and row[3] is None, str(row))
    c.ck("cancel is not length truncation", row and row[4] is None, str(row))

    cancel_calls = []

    def produces():
        time.sleep(0.03)
        return "answer"

    produced = cancel_on_zero_billable(
        produces, 0.005, transport="metered_streaming",
        cancel=lambda: cancel_calls.append(True) or True,
        zero_billable_proven=lambda: False,
    )
    c.ck("token-producing call runs", produced["result"] == "answer")
    c.ck("token-producing outcome is explicit", produced["stall_outcome"] == "produced_tokens_ran")
    c.ck("token-producing call is not cancelled", not cancel_calls)

    os.environ["SPENDGUARD_ESCALATION_RATIO"] = "4"
    at_limit = relative_escalation_gate(1, 4, 10)
    over = relative_escalation_gate(1, 4.01, 10)
    c.ck("ratio<=configured N proceeds", at_limit["proceed"] is True)
    c.ck("ratio>configured N needs auth", over["needs_auth"] is True and over["tripped"] == ["relative"])

    os.environ["SPENDGUARD_ESCALATION_BASELINE_EPSILON"] = "0.001"
    zero_ok = relative_escalation_gate(0, 2, 3)
    zero_cap = relative_escalation_gate(0, 4, 3)
    c.ck("near-zero baseline defers without infinite ratio", zero_ok["proceed"] and zero_ok["ratio"] is None)
    c.ck("near-zero baseline still obeys absolute cap",
         zero_cap["needs_auth"] and zero_cap["tripped"] == ["absolute"])
    refused = relative_escalation_gate(1, 2, 3, quality_bar_met=False)
    c.ck("unmeetable quality bar refuses", not refused["proceed"] and refused["tripped"] == ["quality"])

    print(f"\n{'[FAIL]' if c.fails else 'OK'} test_escalation_ladder_primitives: {c.fails} failure(s)")
    return 1 if c.fails else 0


if __name__ == "__main__":
    sys.exit(main())

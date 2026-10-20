"""Offline guard: lane realtime bursts nudge once per window and never block; metered bursts still block."""
import contextlib
import io
import os
import sys
from _pytest_fixture_bridge import align_test_startup_home

align_test_startup_home(prefix=__name__)
os.environ["SPENDGUARD_NO_AUTOINSTALL"] = "1"
os.environ["SPENDGUARD_TEST_ISOLATED"] = "1"
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from spendguard import bulkgate  # noqa: E402

fails = []


def ck(label, cond, detail=""):
    print(f"  [{'OK' if cond else 'FAIL'}] {label}" + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        fails.append(label)


os.environ["SPENDGUARD_ENFORCE"] = "block"
bulkgate._rt_window.clear()
bulkgate._rt_warned.clear()
stderr = io.StringIO()
raised = []
decision = ""
with contextlib.redirect_stderr(stderr):
    for _ in range(bulkgate.preview_max() + 6):
        try:
            decision = bulkgate.check_realtime("sigA", "m", never_block=True)
        except Exception as exc:
            raised.append(exc)
ck("lanes never block in block mode", not raised, raised)
ck("lane burst returns allow", decision.startswith("allow"), decision)
ck("lane nudge timestamp recorded", bool(bulkgate._rt_warned.get("sigA")))
ck("nudge names Batch API and bulk_delegate", "Batch" in stderr.getvalue() and "bulk_delegate" in stderr.getvalue())
ck("nudge emitted once per window", stderr.getvalue().count("REALTIME BURST") == 1, stderr.getvalue())
ck("every logical call counted", len(bulkgate._rt_window["sigA"]) == bulkgate.preview_max() + 6)

bulkgate._rt_window.clear()
bulkgate._rt_warned.clear()
blocked = False
for _ in range(bulkgate.preview_max() + 1):
    try:
        bulkgate.check_realtime("sigB", "m")
    except bulkgate.GateBlocked:
        blocked = True
        break
ck("metered burst still raises GateBlocked", blocked)

print(f"\n{'[FAIL]' if fails else 'OK'} test_lane_realtime_burst_nudge: {len(fails)} failure(s)")
sys.exit(1 if fails else 0)

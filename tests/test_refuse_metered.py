"""Guard — refuse_metered() / SPENDGUARD_REFUSE_METERED is a hard, fail-closed 'no metered spend' guarantee.

Every metered chokepoint (realtime _rt_precheck_usd + batch _decide) raises MeteredCallRefused when active; it is a
SpendGateRefused subclass, so it PROPAGATES like any deliberate stop. $0 subscription lanes bypass the gate's SDK
path, so they are untouched (not exercised here — this pins the gate switch itself). Pins the context + env switch,
the FAIL-CLOSED env parse (an unrecognised value refuses, never allows), and deliberate-stop membership. Offline."""
import os, sys, tempfile
if not os.environ.get("SPENDGUARD_TEST_ISOLATED"):
    os.environ["SPENDGUARD_TEST_ISOLATED"] = "1"
    os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="sg-refusemeter-")
    _self = os.path.realpath(__file__)
    _root = os.path.realpath(os.path.dirname(__file__)) + os.sep
    if not _self.startswith(_root):
        raise SystemExit("refusing to re-exec a path outside the test directory: %s" % _self)
    os.execv(sys.executable, [sys.executable, _self])

from spendguard import gate

_fails = []
def ck(n, c):
    print(("  [OK] " if c else "  [FAIL] ") + n)
    if not c:
        _fails.append(n)

os.environ.pop("SPENDGUARD_REFUSE_METERED", None)
ck("default: refuse_metered is NOT active", not gate._refuse_metered_active())

print("-- refuse_metered() context: both metered chokepoints raise MeteredCallRefused --")
with gate.refuse_metered():
    ck("active inside the context", gate._refuse_metered_active())
    raised = None
    try:
        gate._rt_precheck_usd("openai", "gpt-5.5", 0.01)
    except gate.MeteredCallRefused:
        raised = "metered"
    ck("a metered REALTIME call raises MeteredCallRefused", raised == "metered")
    r2 = None
    try:
        gate._decide({"provider": "openai", "model": "gpt-5.5", "cost": 0.01, "requests": 1, "in_tok": 10, "out_tok": 10})
    except gate.MeteredCallRefused:
        r2 = "metered"
    ck("a metered BATCH submission raises MeteredCallRefused", r2 == "metered")
ck("NOT active after the context exits (restored)", not gate._refuse_metered_active())

print("-- MeteredCallRefused is a deliberate stop (propagates through fail-open handlers) --")
ck("subclass of SpendGateRefused", issubclass(gate.MeteredCallRefused, gate.SpendGateRefused))
ck("covered by deliberate_stop_types()", isinstance(gate.MeteredCallRefused("x"), gate.deliberate_stop_types()))

print("-- env SPENDGUARD_REFUSE_METERED: process-wide, FAIL-CLOSED (unrecognised → refuse, never allow) --")
for val, expect in [("1", True), ("true", True), ("TRUE", True), ("yes", True), ("on", True), ("enabled", True),
                    ("0", False), ("false", False), ("no", False), ("off", False), ("", False)]:
    if val == "":
        os.environ.pop("SPENDGUARD_REFUSE_METERED", None)
    else:
        os.environ["SPENDGUARD_REFUSE_METERED"] = val
    ck(f"env={val!r} → active={expect}", gate._refuse_metered_active() == expect)
os.environ.pop("SPENDGUARD_REFUSE_METERED", None)

print(("[OK]" if not _fails else "[FAIL]") + " refuse_metered: %d failure(s)" % len(_fails))
sys.exit(1 if _fails else 0)

"""Guard — #2 reasoning-aware DEADLINE floor. A reasoning model needs time to THINK before it writes; until a class is
MEASURED, a tight caller deadline cuts it MID-reasoning — the provider bills the reasoning tokens, no output returns,
the local ledger records $0 (the deadline-cancel waste #3 detects). vendor_call.time_budget therefore floors the
UNMEASURED deadline for a reasoning model at REASONING_DEADLINE_FLOOR_S so the first calls can finish; it only lifts UP
(never shortens a generous default), is clamped to the ceiling, and is replaced by the measured p99 once obs land.

Hermetic: an ISOLATED home so there is NO latency data → time_budget takes the no-data path (reasons_by_default keys off
family rules, which need no DB). TIME twin of #1's bulkgate.reasoning_out_estimate."""
import os
import sys
import tempfile

os.environ.setdefault("SPENDGUARD_TEST_ISOLATED", "1")
os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="spendguard-rdf-")   # no measurements → the no-data path

from spendguard import vendor_call

_fails = []
def ck(label, cond):
    if not cond:
        _fails.append(label)
    print(f"  [{'OK' if cond else 'FAIL'}] {label}")

FLOOR = vendor_call.REASONING_DEADLINE_FLOOR_S
RM = ("openai", "gpt-5.5")            # reasons by FAMILY RULE (needs no DB fact)
NRM = ("openai", "text-embedding-3-large")   # does not reason

# (1) fresh reasoning class + a TIGHT caller default → lifted to the reasoning floor
print("-- (1) unmeasured reasoning class + tight default → floored so it isn't cut mid-thought --")
w, b = vendor_call.time_budget(*RM, sig="fresh:reasoning:xyz", default_s=10)
ck("a tight default (10s) is lifted to the reasoning floor", w == FLOOR)
ck("basis names the reasoning-floor", b == "reasoning-floor")

# (2) fresh reasoning class + a GENEROUS default → unchanged (the floor only lifts UP)
print("-- (2) a generous default is NOT shortened (the floor only ever lifts up) --")
w2, b2 = vendor_call.time_budget(*RM, sig="fresh:reasoning:xyz", default_s=FLOOR + 500)
ck("a generous default is kept", w2 == FLOOR + 500 and b2 == "caller")

# (3) fresh NON-reasoning class + tight default → unchanged (no reasoning floor)
print("-- (3) a non-reasoning model is unaffected --")
w3, b3 = vendor_call.time_budget(*NRM, sig="fresh:embed:xyz", default_s=10)
ck("non-reasoning tight default is unchanged (no floor)", w3 == 10 and b3 == "caller")

# (4) reasoning class, NO caller default → returns the reasoning floor, not (None, 'unknown')
print("-- (4) a reasoning model with no default is seeded (not left 'unknown') --")
w4, b4 = vendor_call.time_budget(*RM, sig="fresh:reasoning:xyz")
ck("no default + reasoning → the reasoning floor (not None/unknown)", w4 == FLOOR and b4 == "reasoning-floor")
w5, b5 = vendor_call.time_budget(*NRM, sig="fresh:embed:xyz")
ck("no default + non-reasoning → (None, 'unknown') unchanged", w5 is None and b5 == "unknown")

# (5) env override honored (knob parity)
print("-- (5) the floor is overridable via env (knob parity) --")
os.environ["SPENDGUARD_ADVISOR_REASONING_DEADLINE_FLOOR_S"] = "250"
try:
    w6, _ = vendor_call.time_budget(*RM, sig="fresh:reasoning:xyz", default_s=10)
    ck("env override raises the floor to 250", w6 == 250)
finally:
    os.environ.pop("SPENDGUARD_ADVISOR_REASONING_DEADLINE_FLOOR_S", None)

print(f"\n{'[FAIL]' if _fails else 'OK'} test_reasoning_deadline_floor: {len(_fails)} failure(s)")
sys.exit(1 if _fails else 0)

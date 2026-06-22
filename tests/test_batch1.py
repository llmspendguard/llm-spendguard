"""Batch-1 gate: a LARGE batch for an intent with no recent realtime/batch-1 test is warned/refused. NO network.

Mechanizes the "test the prompt on a few items before scaling" discipline — the thing the cost cap CAN'T catch
(a buggy-but-cheap batch passes the cap). Covers: refuse (strict, untested) · pass once a realtime test exists ·
GATE_NO_BATCH1 off-switch · no-intent skip · GATE_ALLOW bypass.
"""
import os, sys, tempfile

os.environ["SPENDGUARD_CALLS"] = "1"   # enable the call corpus (the batch-1 signal) — unconditional, so it holds
                                       # whether we re-exec OR the pytest runner already set SPENDGUARD_TEST_ISOLATED
if not os.environ.get("SPENDGUARD_TEST_ISOLATED"):
    os.environ["SPENDGUARD_TEST_ISOLATED"] = "1"
    os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="spendguard-test-")
    os.execv(sys.executable, [sys.executable] + sys.argv)

# stub the Anthropic batch create BEFORE install so the gate wraps the stub (no network)
from anthropic.resources.messages import batches as ab          # noqa: E402
ab.Batches.create = lambda self, *a, **k: "ANTHROPIC_SUBMIT_OK"

from spendguard import gate as spend_gate, calls                # noqa: E402
import anthropic                                                 # noqa: E402

spend_gate.install()
ac = anthropic.Anthropic(api_key="sk-ant-test")
# a "large" batch (≥ GATE_BATCH1_MIN) — small per-request so the COST cap never interferes; this is purely the
# batch-1 check.
reqs = [{"custom_id": f"r{i}", "params": {"model": "claude-opus-4-8",
        "messages": [{"role": "user", "content": "classify x"}], "max_tokens": 10}} for i in range(20)]

for k in ("GATE_ALLOW", "GATE_DISABLE", "GATE_NO_BATCH1", "GATE_REQUIRE_BATCH1"):
    os.environ.pop(k, None)
os.environ["GATE_CAP"] = "100000"      # cost cap wide open — isolate the batch-1 behavior
os.environ["GATE_BATCH1_MIN"] = "5"    # 20 reqs counts as "large"

failures = 0
def check(label, cond):
    global failures
    ok = bool(cond)
    failures += (not ok)
    print(f"  [{'OK' if ok else 'FAIL'}] {label}")

def submit():
    return ac.messages.batches.create(requests=reqs)

def refuses(fn):
    try:
        fn(); return False
    except spend_gate.SpendGateRefused:
        return True

# 1) untested intent + strict → REFUSE
os.environ["GATE_REQUIRE_BATCH1"] = "1"
with calls.context(intent="loinc-typing"):
    check("large untested batch (strict) -> REFUSE", refuses(submit))

# 2) no intent set -> skip (can't reason about shape) -> PASS
check("no intent -> batch-1 check skipped -> PASS", not refuses(submit))

# 3) record a realtime test for the intent -> now it's "tested" -> PASS
calls.record("anthropic", "claude-haiku-4-5", "realtime", 0.001, intent="loinc-typing")  # diff model = fine
check("calls.tested_recently sees the realtime test", calls.tested_recently("loinc-typing"))
with calls.context(intent="loinc-typing"):
    check("tested intent (strict) -> PASS", not refuses(submit))

# 4) a DIFFERENT untested intent still refuses (proves it's per-intent)
with calls.context(intent="brand-new-intent"):
    check("different untested intent (strict) -> REFUSE", refuses(submit))

# 5) GATE_NO_BATCH1 disables the check entirely
os.environ["GATE_NO_BATCH1"] = "1"
with calls.context(intent="brand-new-intent"):
    check("GATE_NO_BATCH1=1 -> check off -> PASS", not refuses(submit))
os.environ.pop("GATE_NO_BATCH1", None)

# 6) GATE_ALLOW bypasses (like the cost cap)
os.environ["GATE_ALLOW"] = "1"
with calls.context(intent="brand-new-intent"):
    check("GATE_ALLOW=1 -> bypass -> PASS", not refuses(submit))
os.environ.pop("GATE_ALLOW", None)

# 7) non-strict default (no GATE_REQUIRE_BATCH1) -> WARN but ALLOW (heuristic, don't break legit jobs)
os.environ.pop("GATE_REQUIRE_BATCH1", None)
with calls.context(intent="another-untested"):
    check("untested, non-strict default -> WARN + ALLOW", not refuses(submit))

print(f"\n{'[FAIL]' if failures else 'OK'} batch-1 gate: {failures} failure(s)")
sys.exit(1 if failures else 0)

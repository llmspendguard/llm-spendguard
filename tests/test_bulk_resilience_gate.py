"""The durable half of chunk-never-single-shot: bulk_delegate REFUSES a large single-shot run with no
crash-resilience, BEFORE any lane is touched.

Why this guard exists: a 54,510-unit build was submitted as ONE bulk call and a transient no-progress pass
killed the whole run on pass 1 — twice. bulk_delegate already CAN chunk + checkpoint (so a crash resumes and a
dead chunk leaves the others complete — pinned in test_bulk_delegate), but nothing stopped a caller from
submitting the fragile shape anyway. This gate makes the fragile shape un-submittable above a threshold:

  (a) over the threshold with NO checkpoint  -> refused (a crash/stall would lose everything);
  (b) over the threshold and chunk_size >= N  -> refused (not actually chunked — one bad unit wedges all);
  (c) the refusal is RAISED before _bulk_arms / any lane work (fail-closed, not a per-task error row);
  (d) a resilient large run (checkpoint + real chunking) passes; and force / small-N / gate-disabled all pass.

Offline: _bulk_arms is stubbed so an ALLOWED run returns immediately without touching a lane; the gate runs
before it, so a REFUSED run never reaches it.
"""
import os
import sys
import tempfile

os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="sg-bulkres-")
os.environ.setdefault("SPENDGUARD_TEST_ISOLATED", "1")
os.environ.setdefault("SPENDGUARD_NO_AUTOINSTALL", "1")
os.environ["SPENDGUARD_BULK_RESILIENCE_MIN_UNITS"] = "5"        # small threshold so the test stays tiny + fast
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from spendguard import lane_balance                                                   # noqa: E402

fails = []


def ck(name, cond):
    ok = bool(cond)
    print(("  [OK] " if ok else "  [FAIL] ") + name)
    if not ok:
        fails.append(name)


# An ALLOWED run must not touch a lane in this test — _bulk_arms=[] makes bulk_delegate return "no viable lane"
# rows immediately (its own early-out), which is proof the RESILIENCE GATE let it through. A REFUSED run raises
# before ever reaching this, so the two cases are unambiguous.
lane_balance._bulk_arms = lambda intent: []

BIG = [f"task-{i}" for i in range(6)]        # 6 > threshold 5
SMALL = [f"task-{i}" for i in range(4)]      # 4 <= threshold 5
CKPT = os.path.join(os.environ["SPENDGUARD_HOME"], "ck.jsonl")


def _raises_refused(**kw):
    try:
        lane_balance.bulk_delegate(BIG, "test:bulk", **kw)
        return None
    except lane_balance.BulkResilienceRefused as e:
        return str(e)
    except Exception as e:
        return e            # wrong type — surfaced so the assertion fails informatively


print("-- (a)/(b) a large single-shot with a resilience gap is REFUSED, and the message names the gap --")
_m1 = _raises_refused(checkpoint=None, chunk_size=100)
ck("no-checkpoint large run raises BulkResilienceRefused", isinstance(_m1, str))
ck("...and the message names the missing checkpoint", isinstance(_m1, str) and "no checkpoint" in _m1)
_m2 = _raises_refused(checkpoint=CKPT, chunk_size=100)      # checkpoint present, but 100 >= 6 → not chunked
ck("checkpointed-but-unchunked large run still raises (chunk_size >= unit count)", isinstance(_m2, str))
ck("...and the message names the chunking gap, NOT the checkpoint",
   isinstance(_m2, str) and "not chunked" in _m2 and "no checkpoint" not in _m2)

print("\n-- (c) the refusal is fail-closed: it RAISES the typed error, before any lane/_bulk_arms work --")
# _bulk_arms returns [] (no lane). If the gate had NOT fired, a refuse-case would RETURN those error rows
# instead of raising — so a raised BulkResilienceRefused proves the gate ran first.
ck("a refused run raises rather than returning rows (gate precedes execution)", isinstance(_m1, str))

print("\n-- (d) a RESILIENT large run passes; and force / small-N / disabled-gate all pass --")
_ok_resilient = lane_balance.bulk_delegate(BIG, "test:bulk", checkpoint=CKPT, chunk_size=2)   # ckpt + real chunks
ck("checkpoint + chunk_size < N (real chunking) is allowed", isinstance(_ok_resilient, list) and len(_ok_resilient) == 6)
_ok_force = lane_balance.bulk_delegate(BIG, "test:bulk", checkpoint=None, chunk_size=100, force=True)
ck("force=True overrides the gate (own the risk)", isinstance(_ok_force, list) and len(_ok_force) == 6)
_ok_small = lane_balance.bulk_delegate(SMALL, "test:bulk", checkpoint=None, chunk_size=100)
ck("a run at/under the threshold is never gated (4 <= 5)", isinstance(_ok_small, list) and len(_ok_small) == 4)

os.environ["SPENDGUARD_BULK_RESILIENCE_MIN_UNITS"] = "0"      # 0 disables the gate entirely
_ok_disabled = lane_balance.bulk_delegate(BIG, "test:bulk", checkpoint=None, chunk_size=100)
ck("bulk.resilience_min_units=0 disables the gate (never refuse on size)", isinstance(_ok_disabled, list) and len(_ok_disabled) == 6)
os.environ["SPENDGUARD_BULK_RESILIENCE_MIN_UNITS"] = "5"

print(f"\n{'[FAIL]' if fails else 'OK'} test_bulk_resilience_gate: {len(fails)} failure(s)")
sys.exit(1 if fails else 0)

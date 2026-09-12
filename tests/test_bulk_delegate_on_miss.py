"""Guard — bulk_delegate's on_miss policy degrades lane misses toward BATCH, not per-task realtime.

The default fallback (refuse_billed=False) is the MOST expensive shape (per-task realtime). A caller with a batch
path wants: lanes first, batch the remainder. Pins (with an always-missing mocked lane, so NOTHING bills):
  · on_miss="batch" + batch_submit → the WHOLE miss set is submitted ONCE, rows become reason="queued_batch" with
    the returned batch handle (async — bulk_delegate never blocks on the 24h window);
  · on_miss="batch" WITHOUT batch_submit → misses become reason="batch_eligible" (a structured routing signal);
  · pairs with return_keyed (queued rows re-associated by key, never position);
  · a DELIBERATE stop from batch_submit PROPAGATES; a plain failure leaves misses batch_eligible (never a false
    "queued");
  · an invalid on_miss raises at the door.
Offline: arms / adapters.call / dispatch are monkeypatched — no lanes, no network, no spend."""
import os, sys, tempfile

if not os.environ.get("SPENDGUARD_TEST_ISOLATED"):
    os.environ["SPENDGUARD_TEST_ISOLATED"] = "1"
    os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="spendguard-onmiss-")
    _self = os.path.realpath(__file__)
    _root = os.path.realpath(os.path.dirname(__file__)) + os.sep
    if not _self.startswith(_root):
        raise SystemExit("refusing to re-exec a path outside the test directory: %s" % _self)
    os.execv(sys.executable, [sys.executable, _self])

from spendguard import lane_balance, lane_catalog, lane_economics, adapters, dispatch, calls, gate

_fails = []
def ck(label, cond):
    if not cond:
        _fails.append(label)
    print(f"  [{'OK' if cond else 'FAIL'}] {label}")

# ── a lane that always MISSES (adapters.call returns an error row, no text) ──
lane_balance._bulk_arms = lambda intent, lanes=None: [("fakelane", "fakemodel")]
lane_catalog.lane_provider = lambda ln: "fakeprov"
lane_economics.prompt_lane_reserved = lambda ln: False
adapters._lane_cooling = lambda ln: False
dispatch.acquire = lambda *a, **k: None
dispatch.release = lambda *a, **k: None
calls.set_context = lambda *a, **k: None
adapters.call = lambda *a, **k: {"error": "lane miss", "reason": "quota"}      # no text → a miss

TASKS = [{"id": "a"}, {"id": "b"}, {"id": "c"}]

print("-- on_miss='batch' + batch_submit: the whole remainder is submitted ONCE, rows are queued_batch --")
_submitted = {"n": 0, "got": None}
def _submit(misses):
    _submitted["n"] += 1
    _submitted["got"] = list(misses)
    return "batch_XYZ"
rows = lane_balance.bulk_delegate(TASKS, "t", on_miss="batch", batch_submit=_submit, force=True)
ck("batch_submit called exactly ONCE (one batch for the remainder)", _submitted["n"] == 1)
ck("the whole miss set was handed to it", len(_submitted["got"]) == 3)
ck("every missed row is queued_batch with the handle",
   all(r.get("reason") == "queued_batch" and r.get("batch") == "batch_XYZ" for r in rows))

print("-- on_miss='batch' WITHOUT batch_submit: misses are batch_eligible (a routing signal) --")
rows2 = lane_balance.bulk_delegate(TASKS, "t", on_miss="batch", force=True)
ck("every missed row is batch_eligible", all(r.get("reason") == "batch_eligible" for r in rows2))
ck("no batch handle when nothing was submitted", all("batch" not in r for r in rows2))

print("-- pairs with return_keyed: queued rows re-associate by key, never position --")
keyed = lane_balance.bulk_delegate(TASKS, "t", on_miss="batch", batch_submit=lambda m: "B", force=True,
                                   task_key=lambda t: t["id"], return_keyed=True)
ck("keyed dict of queued rows", isinstance(keyed, dict) and set(keyed) == {"a", "b", "c"}
   and all(r["reason"] == "queued_batch" for r in keyed.values()))

print("-- a DELIBERATE stop from batch_submit PROPAGATES (never a false 'queued') --")
def _refuse(_m):
    raise gate.SpendGateRefused.__new__(gate.SpendGateRefused)
try:
    lane_balance.bulk_delegate(TASKS, "t", on_miss="batch", batch_submit=_refuse, force=True)
    ck("deliberate stop raised", False)
except gate.SpendGateRefused:
    ck("deliberate stop raised (SpendGateRefused)", True)

print("-- a PLAIN batch_submit failure leaves misses batch_eligible (not falsely queued) --")
def _boom(_m):
    raise RuntimeError("submit endpoint down")
rows3 = lane_balance.bulk_delegate(TASKS, "t", on_miss="batch", batch_submit=_boom, force=True)
ck("a failed submit → misses are batch_eligible, never queued_batch",
   all(r.get("reason") == "batch_eligible" for r in rows3))

print("-- an invalid on_miss raises at the door --")
try:
    lane_balance.bulk_delegate(TASKS, "t", on_miss="teleport", force=True)
    ck("invalid on_miss raised", False)
except ValueError as e:
    ck("invalid on_miss raised ValueError", "on_miss" in str(e))

print(f"\n{'[FAIL]' if _fails else 'OK'} test_bulk_delegate_on_miss: {len(_fails)} failure(s)")
sys.exit(1 if _fails else 0)

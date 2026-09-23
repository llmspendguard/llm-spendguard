"""GUARD — advisor.route_through_queue (Layer 2c): every LABELLED synchronous call is durably RECORDED, flag-gated.

When the flag is ON, a labelled adapters.call is recorded as a leased lane_queue row (observability + crash-recovery
+ priority/SLA metadata) and run via its NORMAL path (model + lane unchanged), then settled. ON by default now the
queue write is POOLED (~46us/call); explicitly OFF it is completely dormant — zero behaviour change, no row. Pins:
  (a) flag EXPLICITLY OFF → the call runs normally and NO durable row is recorded (dormant);
  (a2) flag UNSET → routed (ON is the new default);
  (b) flag ON → a labelled call still runs normally (served) AND exactly ONE durable row is recorded + settled;
  (c) flag ON but UNLABELLED (no intent/sig) → NOT routed (best-value's own rule: never route an unlabelled call);
  (d) flag ON but a _probe call → NOT routed (internal probes stay direct);
  (e) flag ON but already INSIDE a routed record (_route_guard on) → NOT re-recorded — one row per logical call, no
      double-record and no loop (the substitution/fallback recursion boundary).
Hermetic: adapters._call_guarded stubbed; the REAL durable queue in an isolated home; no network, no spend.
"""
import os
import sys
import tempfile

os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="sg-2c-")
os.environ.setdefault("SPENDGUARD_TEST_ISOLATED", "1")
os.environ.setdefault("SPENDGUARD_NO_AUTOINSTALL", "1")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from spendguard import adapters, lane_queue   # noqa: E402

fails = []


def ck(name, cond):
    print(("  [OK] " if cond else "  [FAIL] ") + name)
    if not cond:
        fails.append(name)


def _served(model, prompt, **kw):
    return {"text": "served:" + model, "provider": "acme", "model": (model.split(":", 1)[1] if ":" in model else model),
            "cost": 0.0, "in_tok": 5, "out_tok": 3, "error": None, "executor": "api"}


def _total(d):
    return sum((d or {}).values())


_saved = adapters._call_guarded
adapters._call_guarded = _served

try:
    # ── (a) flag EXPLICITLY OFF → runs normally, NO durable row (dormant) ──
    print("-- (a) flag explicitly OFF: dormant, zero behaviour change --")
    os.environ["SPENDGUARD_ROUTE_THROUGH_QUEUE"] = "0"
    before = lane_queue.queue_depth()
    r = adapters.call("acme:m", "p", intent="cold-intent")
    after = lane_queue.queue_depth()
    ck("flag OFF: the call runs normally (served)", r.get("text") == "served:acme:m")
    ck("flag OFF: NO durable row recorded (dormant)", _total(after) == _total(before))

    # ── (a2) flag UNSET → routed (ON is the new default now the queue write is pooled) ──
    print("\n-- (a2) flag UNSET: routed by default (default is now ON) --")
    os.environ.pop("SPENDGUARD_ROUTE_THROUGH_QUEUE", None)
    b1 = lane_queue.queue_depth()
    adapters.call("acme:m", "p", intent="default-on-intent")
    a1 = lane_queue.queue_depth()
    ck("flag UNSET → routed by default (exactly one durable row)", (a1.get("done", 0) - b1.get("done", 0)) == 1)

    # ── (b) flag ON → a labelled call runs normally AND is recorded once ──
    print("\n-- (b) flag ON: labelled call runs normally + exactly one durable row --")
    os.environ["SPENDGUARD_ROUTE_THROUGH_QUEUE"] = "1"
    b2 = lane_queue.queue_depth()
    r2 = adapters.call("acme:m", "p", intent="test-2c")
    a2 = lane_queue.queue_depth()
    ck("flag ON: the call still runs normally, model/lane unchanged (served)", r2.get("text") == "served:acme:m")
    ck("flag ON: exactly ONE durable row recorded + settled 'done' for the call",
       (a2.get("done", 0) - b2.get("done", 0)) == 1)

    # ── (c) flag ON + UNLABELLED → NOT routed ──
    print("\n-- (c) flag ON but UNLABELLED (no intent/sig) → not routed --")
    b3 = lane_queue.queue_depth()
    adapters.call("acme:m", "p")
    ck("flag ON but UNLABELLED → NOT routed (no row)", _total(lane_queue.queue_depth()) == _total(b3))

    # ── (d) flag ON + probe → NOT routed ──
    print("\n-- (d) flag ON but a _probe call → not routed --")
    b4 = lane_queue.queue_depth()
    adapters.call("acme:m", "p", intent="test-2c", _probe=True)
    ck("flag ON but a probe → NOT routed (no row)", _total(lane_queue.queue_depth()) == _total(b4))

    # ── (e) flag ON + already inside a routed record → NOT re-recorded ──
    print("\n-- (e) flag ON but already inside a routed record (guard on) → not re-recorded --")
    adapters._route_guard.on = True
    b5 = lane_queue.queue_depth()
    adapters.call("acme:m", "p", intent="test-2c")
    a5 = lane_queue.queue_depth()
    adapters._route_guard.on = False
    ck("flag ON but inside a routed record → NOT re-recorded (no double-record / no loop)", _total(a5) == _total(b5))
finally:
    adapters._call_guarded = _saved
    os.environ.pop("SPENDGUARD_ROUTE_THROUGH_QUEUE", None)

print(f"\n{'[FAIL]' if fails else 'OK'} test_route_through_queue: {len(fails)} failure(s)")
sys.exit(1 if fails else 0)

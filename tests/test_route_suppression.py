"""Guard — record_route=False suppresses the DOUBLE-record for work that is ALREADY a durable queue row.

Now that advisor.route_through_queue defaults ON, every LABELLED adapters.call opens a durable queue row (leased→done)
for observability + crash-recovery. But lane_queue.drain / lane_queue.submit run their OWN leased rows THROUGH
lane_balance.bulk_delegate — so without suppression each of those per-task adapters.call would open a SECOND row for
work that is already a queue row (the double-record). record_route=False (set only by drain/submit) threads through
bulk_delegate → adapters.call as `_route=False`, which forces `_routed`=False so no second row is opened.

  (1) adapters.call(_route=False) does NOT call record_open even when routing is ENABLED; _route=True (default) DOES.
  (2) bulk_delegate(record_route=False) threads _route=False into EVERY per-task adapters.call (pinned runner), so a
      drain/submit-driven fan never double-records; the default (record_route=True) threads _route=True.

Hermetic: _call_guarded, the resolver, dispatch admission, and lane_queue.record_* are stubbed — no network, no db."""
import os
import sys
import tempfile

os.environ.setdefault("SPENDGUARD_TEST_ISOLATED", "1")
os.environ.setdefault("SPENDGUARD_HOME", tempfile.mkdtemp(prefix="spendguard-route-"))

from spendguard import adapters, dispatch, lane_balance, lane_queue

_fails = []
def ck(label, cond):
    if not cond:
        _fails.append(label)
    print(f"  [{'OK' if cond else 'FAIL'}] {label}")

# ── (1) adapters.call: the _route flag gates the durable route-through-queue record ──
print("-- (1) adapters.call(_route=False) suppresses the route-through-queue record (routing forced ON) --")
_opened = []
_saved = (lane_queue.record_open, lane_queue.record_close,
          adapters._call_guarded, adapters._route_through_queue_enabled)
lane_queue.record_open = lambda intent, prompt: (_opened.append(intent) or 123)
lane_queue.record_close = lambda qrid, r: None
adapters._route_through_queue_enabled = lambda: True     # force routing ON regardless of config
adapters._resolve_guard.on = True                        # skip the served-substitute resolver (no network)
def _stub_guarded(model, prompt, **kw):
    return {"text": "ok", "model": model, "provider": "x", "cost": 0.0, "in_tok": 1, "out_tok": 1, "error": None}
adapters._call_guarded = _stub_guarded
try:
    adapters.call("openai:x", "p", sig="rt:test", _route=True)
    ck("_route=True (default) + routing ON → record_open called once", _opened == ["rt:test"])
    _opened.clear()
    adapters.call("openai:x", "p", sig="rt:test", _route=False)
    ck("_route=False → record_open NOT called (no double-record)", _opened == [])
finally:
    (lane_queue.record_open, lane_queue.record_close,
     adapters._call_guarded, adapters._route_through_queue_enabled) = _saved
    adapters._resolve_guard.on = False

# ── (2) bulk_delegate(record_route=False) threads _route=False into every per-task adapters.call ──
print("-- (2) bulk_delegate(record_route=False) → per-task adapters.call gets _route=False (pinned runner) --")
_seen_route = []
_saved_call = adapters.call
_saved_acq = dispatch.acquire_or_none
_saved_rel = dispatch.release
def _capture_call(model, prompt, **kw):
    _seen_route.append(kw.get("_route"))
    return {"text": "ok", "model": model, "provider": "openai", "cost": 0.0, "in_tok": 1, "out_tok": 1,
            "error": None, "executor": "api"}
dispatch.acquire_or_none = lambda *a, **k: object()   # a non-None governor token (slot granted)
dispatch.release = lambda *a, **k: None
adapters.call = _capture_call
try:
    lane_balance.bulk_delegate([{"id": "a"}, {"id": "b"}], "rt:bulk",
                               model_for=lambda t: "openai:gpt-x", record_route=False, force=True)
    ck("both drain/submit tasks called adapters.call with _route=False", _seen_route == [False, False])
    _seen_route.clear()
    lane_balance.bulk_delegate([{"id": "a"}], "rt:bulk", model_for=lambda t: "openai:gpt-x", force=True)
    ck("default record_route=True → per-task _route=True (normal observability)", _seen_route == [True])
finally:
    adapters.call = _saved_call
    dispatch.acquire_or_none = _saved_acq
    dispatch.release = _saved_rel

print(f"\n{'[FAIL]' if _fails else 'OK'} test_route_suppression: {len(_fails)} failure(s)")
sys.exit(1 if _fails else 0)

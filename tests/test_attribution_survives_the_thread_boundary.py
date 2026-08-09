"""A call's INTENT must reach the ledger, including when the call runs on a worker thread.

WHY THIS GUARD EXISTS. Measured over a real 535-call, $25.29 session: 533 calls had NO intent. The money
could be totalled and not explained, which is the failure this whole project exists to prevent — the core
mission is ATTRIBUTION, not arithmetic.

Two causes, and both were mine:

  1. vendor_call recorded `purpose` on 400/400 of its own results and never passed it to the ledger. The
     information existed the whole time and did not reach the consumer.
  2. calls.record() reads intent from a THREAD-LOCAL context, and _attempt runs the adapter on a WORKER.
     So the tag was set on the calling thread and the row written on another one with an empty context.
     "Thread-local; safe under ThreadPool" is true for isolation and exactly wrong for propagation — and
     the same thread boundary made `caller` read `threading.py:run:1024`, the worker frame, for 450 calls
     and $24.66 of spend.

Concurrency added for SPEED silently destroyed the only attribution there was.
"""
import inspect
import sys

from spendguard import calls, vendor_call as vc

failures = 0


def check(label, ok, extra=""):
    global failures
    if not ok:
        failures += 1
    print(f"  [{'OK' if ok else 'FAIL'}] {label}" + (f"  — {extra}" if not ok and extra else ""))


def test_call_tags_the_ledger_with_its_purpose():
    src = inspect.getsource(vc.call)
    check("vendor_call.call opens a calls.context from `purpose`",
          "calls as _calls" in src and "context(intent=purpose" in src,
          "a purpose recorded only on the vendor_call log explains nothing in the ledger")


def test_the_context_is_carried_onto_the_worker_thread():
    src = inspect.getsource(vc._attempt)
    check("_attempt captures the parent context before starting the thread", "_parent_ctx" in src)
    check("...and re-establishes it INSIDE the worker", "set_context(intent=_parent_ctx" in src,
          "otherwise the row is written on a thread whose thread-local context is empty")


def test_context_really_does_not_cross_threads_by_itself():
    """The premise, asserted rather than assumed: if this ever became inheritable, the fix above would be
    dead weight and someone would remove it for the wrong reason."""
    import threading
    seen = {}
    calls.set_context(intent="parent-intent", chain="parent-chain")

    def worker():
        seen["intent"] = (calls.current() or {}).get("intent")

    t = threading.Thread(target=worker)
    t.start()
    t.join()
    check("a plain worker thread does NOT inherit the context", seen.get("intent") != "parent-intent",
          f"worker saw {seen.get('intent')!r} — the propagation fix would be unnecessary")


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print(f"\n{'[FAIL]' if failures else 'OK'} test_attribution_survives_the_thread_boundary: {failures} failure(s)")
    sys.exit(1 if failures else 0)

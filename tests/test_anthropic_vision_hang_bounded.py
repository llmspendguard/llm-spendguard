"""FOLLOW-UP to a34ef34 (an explicit timeout_s no longer breaks the Anthropic VISION path). a34ef34 OMITS the httpx
client timeout for an images= call — so the ONLY remaining wall-clock bound on a vision request is the daemon-thread
join + c.close() in _call_once._anth_msg. That bound must be PROVEN, not assumed: removing the httpx timeout must not
have removed the ceiling. This guard pins two things a34ef34's success-path test does not:

  PART 2 — HANG-STILL-BOUNDED. A fake Anthropic transport whose streamed read BLOCKS far past the deadline is still cut
  at ~timeout_s with a _CallDeadline (NOT a hang), AND its client was built WITHOUT the httpx `timeout` (the omitted-
  vision path) AND c.close() was invoked (the real billing-cancel). So the daemon-join alone bounds vision.

  PART 3 — SURFACE PARITY. The named vision entry adapters.vision(images=…, timeout_s=…) inherits the fix (its client
  also OMITS `timeout`) because it funnels through the same call()→_call_once.

Surface parity for the OTHER entries is structural, not something a unit test re-proves per surface (verified by reading,
recorded in docs/VISION.md): adapters.call(images=…), bulk_delegate(images_for=…) via _run_task_on_api, and
crossllm.ask_vision (which calls bulk_delegate) all reach the SAME _call_once through adapters.call(images=…). The
Anthropic BATCH submit path (experiment._promote_batch / submit.guarded_submit) constructs timeout-FREE clients — it
never hands the SDK a per-call httpx timeout, so it was never subject to this trap. A REGRESSION that re-adds an
unconditional httpx timeout to _call_once is caught behaviorally by tests/test_anthropic_vision_timeout.py (its "the
vision client OMITS `timeout`" assertion fails the moment the timeout stops being images-conditioned) — so that class of
regression is already un-regressable without a fragile source-scan here.

Offline, no network, no spend. Isolated SPENDGUARD_HOME.
"""
import os
import sys
import tempfile
import threading
import time

os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="sg-anthropic-vision-hang-")
os.environ.setdefault("SPENDGUARD_TEST_ISOLATED", "1")
os.environ.setdefault("SPENDGUARD_NO_AUTOINSTALL", "1")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

import anthropic                                                    # noqa: E402
from spendguard import adapters                                     # noqa: E402

PNG = ("data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==")
# the RAW path _call_once() expects images ALREADY loaded (the public call() loads them before dispatch), so preload for
# the direct Part-2 call; Part 3 hands the raw data URL to adapters.vision(), whose public path loads it itself.
LOADED = adapters._load_image(PNG)
MODEL = "anthropic:claude-opus-4-8"
BLOCK_S = 30.0                                                      # the transport "hangs" this long — far past the deadline
TIMEOUT_S = 1.0                                                     # the wall-clock deadline the daemon-join must enforce


def main():
    fails = []

    def ck(name, cond):
        print(("  [OK] " if cond else "  [FAIL] ") + name)
        if not cond:
            fails.append(name)

    init_kwargs = []                                               # what each fake Anthropic client was constructed with

    class _FakeUsage:
        input_tokens = 10
        output_tokens = 5
        cache_creation_input_tokens = 0
        cache_read_input_tokens = 0

    class _FakeBlock:
        type = "text"
        text = "a faithful one-sentence caption of the image"

    class _FakeMessage:
        content = [_FakeBlock()]
        usage = _FakeUsage()
        stop_reason = "end_turn"
        model = "claude-opus-4-8"

    # ── PART 2: a transport that BLOCKS the streamed read until the client is closed (the deadline cancel) or BLOCK_S ──
    class _BlockingStream:
        def __init__(self, closed):
            self._closed = closed

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def get_final_message(self):
            # models a large vision response whose body never completes: it returns only when c.close() fires the event
            # (the real deadline cancel) or BLOCK_S elapses. The join on the main thread is what must cut this at timeout_s.
            if self._closed.wait(BLOCK_S):
                raise RuntimeError("connection closed by client (deadline cancel)")
            return _FakeMessage()

    class _BlockingMessages:
        def __init__(self, closed):
            self._closed = closed

        def stream(self, **kw):
            return _BlockingStream(self._closed)

    class _BlockingAnthropic:
        def __init__(self, **kwargs):
            init_kwargs.append(kwargs)
            self._closed = threading.Event()
            self.messages = _BlockingMessages(self._closed)
            self.closed_flag = {"closed": False}
            _BlockingAnthropic.last = self

        def close(self):
            self.closed_flag["closed"] = True
            self._closed.set()                                     # unblock the daemon read (models the connection teardown)

    print("-- PART 2: a BLOCKING vision transport is cut at the deadline (bounded _CallDeadline), not a hang --")
    init_kwargs.clear()
    anthropic.Anthropic = _BlockingAnthropic
    t0 = time.time()
    rv = adapters._call_once(MODEL, "Describe the image.", max_tokens=64, timeout_s=TIMEOUT_S,
                             images=[LOADED], _skip_lane=True)        # raw metered path: isolates the daemon-join bound
    elapsed = time.time() - t0
    ck("a blocking vision call is BOUNDED (returned well before the transport's %.0fs block)" % BLOCK_S,
       elapsed < BLOCK_S * 0.5)
    ck("it actually waited ~the deadline (the join fired — not an instant unrelated error)", elapsed >= TIMEOUT_S * 0.5)
    ck("the outcome is a _CallDeadline (a wall-clock deadline, not a generic transport error)",
       rv.get("error_type") == "_CallDeadline")
    ck("the deadline reason is surfaced whole", "deadline_exceeded" in (rv.get("error") or ""))
    ck("the vision client was built WITHOUT the httpx `timeout` (the omitted-vision path a34ef34 introduced)",
       bool(init_kwargs) and "timeout" not in init_kwargs[-1])
    ck("c.close() WAS invoked (the real billing-cancel — vision keeps the cancel even without the httpx timeout)",
       getattr(_BlockingAnthropic, "last", None) is not None and _BlockingAnthropic.last.closed_flag["closed"])

    # ── PART 3: the NAMED vision entry inherits the fix (funnels through call()→_call_once) ──
    class _OkStream:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def get_final_message(self):
            return _FakeMessage()

    class _OkMessages:
        def stream(self, **kw):
            return _OkStream()

    class _OkAnthropic:
        def __init__(self, **kwargs):
            init_kwargs.append(kwargs)
            self.messages = _OkMessages()

        def close(self):
            pass

    print("\n-- PART 3: adapters.vision(images=…, timeout_s=…) inherits the omit-timeout fix --")
    init_kwargs.clear()
    anthropic.Anthropic = _OkAnthropic
    rvn = adapters.vision(MODEL, "Describe the image.", [PNG], timeout_s=120)
    ck("the named vision entry succeeds (returns a caption)", not rvn.get("error") and bool(rvn.get("text")))
    ck("its client OMITS `timeout` too (adapters.vision → call → _call_once, same path)",
       bool(init_kwargs) and "timeout" not in init_kwargs[-1])

    print(f"\n{'[FAIL]' if fails else 'OK'} test_anthropic_vision_hang_bounded: {len(fails)} failure(s)")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())

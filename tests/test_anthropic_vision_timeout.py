"""REGRESSION GUARD — an explicit `timeout_s` must NOT break the Anthropic VISION path.

The trap (2026-09-27): `adapters.call(images=…, timeout_s=120)` handed the Anthropic SDK an httpx client timeout, and
on a vision request (large image body) that surfaced as "Connection error." → the call returned None. A whole bakeoff
slate of claude-opus-4-8 failed 40/40 on `7thsense-vision-caption` while gpt-5-nano/mini and gemini succeeded; the
known-good production caller (7thsense vision/openai_backend.py) had ALREADY learned to never forward `timeout` for this
reason. The fix (adapters._call_once): for an `images=` call, keep the wall-clock deadline via the daemon-thread join +
c.close() (which cancels billing) but OMIT the httpx client timeout that breaks vision. Text keeps the httpx timeout.

This test fakes the Anthropic SDK client (no network, no spend) and pins BOTH halves: a vision call with timeout_s
SUCCEEDS (not None / not "Connection error.") and its client is built WITHOUT `timeout`; a text call with the same
timeout_s still passes `timeout` (unchanged). Offline, isolated home.
"""
import os
import sys
import tempfile

os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="sg-anthropic-vision-timeout-")
os.environ.setdefault("SPENDGUARD_TEST_ISOLATED", "1")
os.environ.setdefault("SPENDGUARD_NO_AUTOINSTALL", "1")
# Offline scripts must supply their OWN fake key: keyless CI has no ANTHROPIC_API_KEY, so without this the metered path
# short-circuits at the "no key" check BEFORE the monkeypatched client is used, and every assertion cascades (the
# green-locally/red-on-keyless-CI trap — the client is faked below + test_runner points a dead proxy at any real call).
os.environ.setdefault("ANTHROPIC_API_KEY", "sk-ant-test-offline")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

import anthropic                                                    # noqa: E402
from spendguard import adapters                                     # noqa: E402

# a real, tiny 1x1 PNG data URL — adapters._load_image parses it (reads dims), so the vision path runs for real
PNG = ("data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==")
MODEL = "anthropic:claude-opus-4-8"                                 # a real catalog model, so pricing/context resolve


def main():
    fails = []

    def ck(name, cond):
        print(("  [OK] " if cond else "  [FAIL] ") + name)
        if not cond:
            fails.append(name)

    init_kwargs = []                                               # local: what each fake Anthropic client was built with

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

    class _FakeStream:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def get_final_message(self):
            return _FakeMessage()

    class _FakeMessages:
        def stream(self, **kw):
            return _FakeStream()

    class _FakeAnthropic:
        def __init__(self, **kwargs):
            init_kwargs.append(kwargs)                             # closes over the local list — no module-global mutation
            self.messages = _FakeMessages()

        def close(self):
            pass

    anthropic.Anthropic = _FakeAnthropic                          # monkeypatch the SDK class (test setup, like the other vision tests)

    print("-- a VISION call with timeout_s: succeeds AND is built WITHOUT the httpx client timeout --")
    init_kwargs.clear()
    rv = adapters.call(MODEL, "Describe the image.", images=[PNG], timeout_s=120)
    ck("vision call did NOT collapse to error/None (returns text)", not rv.get("error") and bool(rv.get("text")))
    ck("an Anthropic client was constructed", len(init_kwargs) >= 1)
    ck("the vision client OMITS `timeout` (the httpx timeout that breaks vision)",
       bool(init_kwargs) and "timeout" not in init_kwargs[-1])

    print("\n-- a TEXT call with the same timeout_s: unchanged, still passes the httpx client timeout --")
    init_kwargs.clear()
    rt = adapters.call(MODEL, "Say hello.", timeout_s=120)
    ck("text call succeeds", not rt.get("error") and bool(rt.get("text")))
    ck("the text client still receives `timeout` (fast-connect cancel preserved for text)",
       bool(init_kwargs) and "timeout" in init_kwargs[-1])

    print(f"\n{'[FAIL]' if fails else 'OK'} test_anthropic_vision_timeout: {len(fails)} failure(s)")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())

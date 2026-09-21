"""GUARD — the .stream() wrapper dispatch, so a streamed call is recorded EXACTLY ONCE (never zero, never twice).

Two kinds of SDK streaming helper, and the distinction is load-bearing:
  • OpenAI `.stream()` builds partial(self.create, …) → it calls the ALREADY-GATED create(stream=True), which
    records the call. Wrapping .stream() to ALSO record double-counts (measured: two identical ledger rows for one
    Responses.stream call). So OpenAI stream entries are _STREAM_PASSTHROUGH: marked _spend_gated for the surface
    sweep, but the SDK manager is returned UNCHANGED — no second recorder.
  • Anthropic `.stream()` hits self._post DIRECTLY, bypassing create (the 2,921-call leak) → its manager is the
    SOLE recorder, so it must be WRAPPED.

This pins that dispatch. The metering itself (OpenAI via create; Anthropic via the manager) is covered by
test_stream_capture + the ground-truth probe. Offline, no SDK import, no network, no ledger I/O."""
import os
import sys
import tempfile

os.environ.setdefault("SPENDGUARD_TEST_ISOLATED", "1")
os.environ.setdefault("SPENDGUARD_HOME", tempfile.mkdtemp(prefix="spendguard-streamdispatch-"))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from spendguard import gate   # noqa: E402

fails = []


def ck(name, cond):
    print(("  [OK] " if cond else "  [FAIL] ") + name)
    if not cond:
        fails.append(name)


class _Mgr:                                          # a stand-in SDK stream manager (what .stream() returns)
    pass


def _orig(self, *a, **k):
    return _Mgr()


print("-- OpenAI .stream() is PASSTHROUGH: gated, manager returned UNCHANGED (inner create is the sole recorder) --")
w = gate._wrap_stream(_orig, gate._STREAM_PASSTHROUGH, None, None)
ck("sync passthrough is marked _spend_gated (surface sweep is satisfied)", getattr(w, "_spend_gated", False) is True)
ck("sync passthrough returns the ORIGINAL manager, not a recording proxy", type(w(object(), model="gpt-5.5")) is _Mgr)
wa = gate._wrap_async_stream(_orig, gate._STREAM_PASSTHROUGH, None, None)
ck("async passthrough is marked _spend_gated", getattr(wa, "_spend_gated", False) is True)
ck("async passthrough returns the ORIGINAL manager", type(wa(object(), model="gpt-5.5")) is _Mgr)

print("\n-- Anthropic .stream() RECORDS: manager is WRAPPED in a recording proxy (it bypasses create) --")
w2 = gate._wrap_stream(_orig, "get_final_message", gate._est_anth_msg, gate._act_anth_msg)
ck("sync record is marked _spend_gated", getattr(w2, "_spend_gated", False) is True)
ck("sync record WRAPS the manager (a recording proxy, not the original)",
   type(w2(object(), model="claude-opus-4-8")) is not _Mgr)
wa2 = gate._wrap_async_stream(_orig, "get_final_message", gate._est_anth_msg, gate._act_anth_msg)
ck("async record WRAPS the manager", type(wa2(object(), model="claude-opus-4-8")) is not _Mgr)

print(f"\n{'[FAIL]' if fails else 'OK'} test_stream_wrap_dispatch: {len(fails)} failure(s)")
sys.exit(1 if fails else 0)

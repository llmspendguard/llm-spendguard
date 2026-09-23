"""Guard — #3 DEADLINE-CANCEL waste detection. A call torn down at its wall-clock deadline MID-generation (c.close())
bills at the provider for what it generated — for a reasoning model, reasoning tokens that produced NO output — but
returns no usage, so the LOCAL result is out_tok=0/cost=0 and the ledger records $0. That waste is invisible per-call;
spendguard must SURFACE it. bulkgate.note_deadline_cancel counts per model + announces at decade boundaries, and
_call_once fires it on a real timeout cancel.

Offline: a fake OpenAI client whose create() hangs past the deadline (mirrors test_call_deadline_bounds_hang)."""
import io
import os
import sys
import tempfile
import time
import types
from contextlib import redirect_stderr

os.environ.setdefault("SPENDGUARD_TEST_ISOLATED", "1")
os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="spendguard-dc-")

import openai  # noqa: E402
from spendguard import adapters, bulkgate, config, vendor_call  # noqa: E402

_fails = []
def ck(label, cond):
    if not cond:
        _fails.append(label)
    print(f"  [{'OK' if cond else 'FAIL'}] {label}")

# ── (1) the detector: counts every cancel, announces only at decade boundaries, names the invisible waste ──
print("-- (1) note_deadline_cancel: counts per model, announces at decade boundaries (1,10,...), names the waste --")
bulkgate._cancel_warned.pop("openai:gpt-5.5", None)
buf = io.StringIO()
with redirect_stderr(buf):
    for _ in range(11):
        bulkgate.note_deadline_cancel("openai:gpt-5.5", 60)
ck("every cancel is counted (11 calls → count 11)", bulkgate._cancel_warned.get("openai:gpt-5.5") == 11)
out = buf.getvalue()
ck("announced at the 1st and 10th only (decade boundaries), not all 11", out.count("DEADLINE-CANCELLED") == 2)
ck("the message names the invisible waste (local $0 vs provider bills) and reasoning",
   ("ledger records $0" in out or "invisible" in out.lower()) and "reasoning" in out.lower())
ck("the message carries an actionable fix (more deadline / lower concurrency)", "Fix:" in out and "deadline" in out.lower())

# ── (2) end-to-end: a hanging _call_once fires note_deadline_cancel on the timeout cancel ──
print("-- (2) _call_once fires note_deadline_cancel when it tears down a hung call at the deadline --")


class _HangClient:
    """create() sleeps past the deadline so _call_once cancels it (no with_raw_response → the plain fallback path)."""
    def __init__(self, **_kw):
        pass

    def _create(self, **_kw):
        time.sleep(10.0)
        return types.SimpleNamespace(
            choices=[types.SimpleNamespace(message=types.SimpleNamespace(content="ok"), finish_reason="stop")],
            usage=types.SimpleNamespace(prompt_tokens=5, completion_tokens=3))

    @property
    def chat(self):
        return types.SimpleNamespace(completions=types.SimpleNamespace(create=self._create))

    def close(self):
        pass


openai.OpenAI = lambda **kw: _HangClient()
config.api_key = lambda name: "sk-fake"
vendor_call.served_check = lambda prov, raw: "unchecked"

bulkgate._cancel_warned.pop("openai:gpt-5.5", None)
t0 = time.time()
r = adapters._call_once("openai:gpt-5.5", "hi", max_tokens=100, timeout_s=1)
ck("the hung call was bounded at ~timeout_s (not the 10s hang)", time.time() - t0 < 4.0)
ck("it returned a deadline error (no fabricated result)", bool(r.get("error")))
ck("_call_once fired note_deadline_cancel on the cancel → the invisible waste is now COUNTED",
   bulkgate._cancel_warned.get("openai:gpt-5.5", 0) >= 1)

print(f"\n{'[FAIL]' if _fails else 'OK'} test_deadline_cancel_waste: {len(_fails)} failure(s)")
sys.exit(1 if _fails else 0)

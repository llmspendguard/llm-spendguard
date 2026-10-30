"""Guard — #3 DEADLINE-CANCEL waste detection. A call torn down at its wall-clock deadline MID-generation (c.close())
bills at the provider for what it generated — for a reasoning model, reasoning tokens that produced NO output — but
returns no usage, so the LOCAL result is out_tok=0/cost=0 and the ledger records $0. That waste is invisible per-call;
spendguard must SURFACE it. bulkgate.note_deadline_cancel counts per model + announces at decade boundaries, and
_call_once fires it on a real timeout cancel.

Offline: a fake OpenAI client yields past a synthetic deadline (mirrors test_call_deadline_bounds_hang)."""
import io
import os
import sys
import tempfile
import types
from contextlib import redirect_stderr
from unittest.mock import patch

os.environ.setdefault("SPENDGUARD_TEST_ISOLATED", "1")
os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="spendguard-dc-")

import openai  # noqa: E402
from spendguard import adapters, budget, bulkgate, calls, config, vendor_call  # noqa: E402
from deadline_test_timeline import AdapterDeadlineTimeline  # noqa: E402

CANCEL_DEADLINE_S = 1.0  # Synthetic time; no host-latency assertion.
CANCEL_PROVIDER_PENDING_S = CANCEL_DEADLINE_S * 10
CANCEL_LEDGER_INTENT = "test:deadline-cancel-local-ledger"
_cancel_timeline = AdapterDeadlineTimeline()
adapters.threading = _cancel_timeline.thread_facade(adapters.threading)

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
    """Synthetic pending create; no with_raw_response exercises the plain fallback."""
    def __init__(self, **_kw):
        pass

    def _create(self, **_kw):
        _cancel_timeline.provider_delay(CANCEL_PROVIDER_PENDING_S)
        return types.SimpleNamespace(
            choices=[types.SimpleNamespace(message=types.SimpleNamespace(content="ok"), finish_reason="stop")],
            usage=types.SimpleNamespace(prompt_tokens=5, completion_tokens=3))

    @property
    def chat(self):
        return types.SimpleNamespace(completions=types.SimpleNamespace(create=self._create))

    def close(self):
        _cancel_timeline.client_closed()


openai.OpenAI = lambda **kw: _HangClient()
config.api_key = lambda name: "sk-fake"
vendor_call.served_check = lambda prov, raw: "unchecked"

bulkgate._cancel_warned.pop("openai:gpt-5.5", None)
r = adapters._call_once("openai:gpt-5.5", "hi", max_tokens=100, timeout_s=CANCEL_DEADLINE_S)
ck("the hung call joined and closed exactly at its deadline",
   _cancel_timeline.joins == [CANCEL_DEADLINE_S] and _cancel_timeline.closed_at == [CANCEL_DEADLINE_S])
ck("it returned a deadline error (no fabricated result)", r.get("error_type") == "_CallDeadline")
ck("cancel returned no usage, text or locally billed cost",
   not r.get("text") and not r.get("in_tok") and not r.get("out_tok") and not r.get("cost"))
ck("_call_once fired note_deadline_cancel on the cancel → the invisible waste is now COUNTED",
   bulkgate._cancel_warned.get("openai:gpt-5.5", 0) >= 1)

# Exercise the real outer failure recorder as well: _call_once alone does not write
# an API failure row. Bypass admission/retry policy, retaining the synthetic SDK
# deadline and the production forensic writer that converts absent local cost to $0.
def _cancel_ledger_guarded_call(model, prompt, **kwargs):
    return adapters._call_once(model, prompt, max_tokens=kwargs["max_tokens"],
                               timeout_s=kwargs["timeout_s"], _skip_lane=True)


_cancel_billed_before = budget.spent_all_time()
with calls.context(intent=CANCEL_LEDGER_INTENT), \
        patch.object(adapters, "_call_guarded", _cancel_ledger_guarded_call), \
        patch.object(adapters, "_manage_all_enabled", return_value=False):
    _cancel_recorded_result = adapters.call(
        "openai:gpt-5.5", "hi", max_tokens=100, timeout_s=CANCEL_DEADLINE_S,
        intent=CANCEL_LEDGER_INTENT, reasoning="low", metered_only=True,
        base_fallback=False, _route=False, _coalesce_eligible=False)
_cancel_ledger_rows = calls._calls_db().execute(
    "SELECT cost,in_tok,out_tok,disposition FROM calls WHERE intent=?",
    (CANCEL_LEDGER_INTENT,)).fetchall()
ck("deadline failure has exactly one local $0 row with zero usage and failed disposition",
   [tuple(row) for row in _cancel_ledger_rows] == [(0.0, 0, 0, "failed")]
   and _cancel_recorded_result.get("error_type") == "_CallDeadline")
ck("deadline with no returned usage adds no local billed spend",
   budget.spent_all_time() == _cancel_billed_before)

print(f"\n{'[FAIL]' if _fails else 'OK'} test_deadline_cancel_waste: {len(_fails)} failure(s)")
sys.exit(1 if _fails else 0)

"""Guardrail B — EFFORT IS PATH-INDEPENDENT. The SAME (model, intent, reasoning) must resolve to the SAME wire effort
whether the call goes through the plain path, the governed=True dispatch path, or the bulk_delegate pinned fan. Incident
2: an earlier gpt-5.5 pass with governed=True ran at a different effort than the plain path (~380 calls cut at the
deadline, $50.82 for null results). The three doors now converge on ONE resolver in adapters._call_once — this test
PROVES it and FAILS if a refactor ever reintroduces a per-path effort divergence (the spec's 'prose alone does not
count'). Acceptance test from docs/GUARDRAILS_reasoning_overspend.md §2.

Faithful: the REAL adapters.call effort resolution runs on every path through a fake OpenAI client; only the governor
(admit/acquire/release) is stubbed to admit — it never touches reasoning, so the effort each path sends is the real one.
Offline (no network)."""
import os
import sys
import tempfile
import types

os.environ.setdefault("SPENDGUARD_TEST_ISOLATED", "1")
os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="spendguard-pathindep-")
os.environ["SPENDGUARD_DISPATCH_XP_OFF"] = "1"
os.environ["SPENDGUARD_ROUTE_THROUGH_QUEUE"] = "0"

import openai  # noqa: E402
from spendguard import adapters, config, dispatch, lane_balance, vendor_call  # noqa: E402

_fails = []
def ck(label, cond):
    if not cond:
        _fails.append(label)
    print(f"  [{'OK' if cond else 'FAIL'}] {label}")

_COMPLETION = types.SimpleNamespace(
    choices=[types.SimpleNamespace(message=types.SimpleNamespace(content="ok"), finish_reason="stop")],
    usage=types.SimpleNamespace(prompt_tokens=5, completion_tokens=3))

_WIRE = []   # every kwargs dict that reached the vendor create() — so each path's reasoning_effort is the REAL one sent


class _CaptureClient:
    def __init__(self, **_kw):
        pass

    def _capture(self, **kw):
        _WIRE.append(dict(kw))
        return _COMPLETION

    def _raw_capture(self, **kw):
        _WIRE.append(dict(kw))
        return types.SimpleNamespace(headers={}, parse=lambda: _COMPLETION)

    @property
    def chat(self):
        wr = types.SimpleNamespace(create=self._raw_capture)
        return types.SimpleNamespace(completions=types.SimpleNamespace(create=self._capture, with_raw_response=wr))

    def close(self):
        pass


config.api_key = lambda name: "sk-fake"
vendor_call.served_check = lambda prov, raw: "unchecked"
openai.OpenAI = lambda **kw: _CaptureClient()
# Stub ONLY the governor: admit/acquire always grant, release is a no-op. None of these touch `reasoning`, so the wire
# effort each path produces is the genuine resolver output — the stub isolates path-independence, it does not fake it.
dispatch.admit = lambda *a, **k: types.SimpleNamespace(ok=True, shed=False, error=None, release=lambda: None)
dispatch.acquire_or_none = lambda *a, **k: object()
dispatch.release = lambda *a, **k: None


def _last_wire_effort():
    return (_WIRE[-1] or {}).get("reasoning_effort") if _WIRE else "<no-call>"


def _plain(model, reasoning):
    _WIRE.clear()
    adapters.call(model, "hi", reasoning=reasoning, intent="pathindep", max_tokens=100, timeout_s=30)
    return _last_wire_effort()


def _governed(model, reasoning):
    _WIRE.clear()
    adapters.call(model, "hi", reasoning=reasoning, intent="pathindep", max_tokens=100, timeout_s=30, governed=True)
    return _last_wire_effort()


def _fan(model, reasoning):
    _WIRE.clear()
    # prompt_for pins the SAME prompt "hi" the plain/governed paths send, so the three paths differ ONLY in the door
    # they take, never in their inputs (without it the opaque task dict would become the prompt).
    lane_balance.bulk_delegate([{"id": 0}], "pathindep", reasoning=reasoning,
                               model_for=lambda t: model, prompt_for=lambda t: "hi",
                               chunk_size=1, force=True, deadline_s=30.0)
    return _last_wire_effort()


# Each model is a DIFFERENT resolution outcome, so the test proves parity of the RESOLVER, not just of one constant:
#   gpt-5.5   floor='none'    → 'minimal' pin CANNOT be honored → all paths must send 'none' (the $45/$50 incident model)
#   gpt-5-nano floor='minimal' → 'minimal' pin IS honored        → all paths must send 'minimal'
for model, pin, expect in [("openai:gpt-5.5", "minimal", "none"),
                           ("openai:gpt-5-nano", "minimal", "minimal"),
                           ("openai:gpt-5.5", "high", "high")]:
    print(f"-- {model} + reasoning='{pin}' → identical wire effort on plain / governed / fan (expect '{expect}') --")
    e_plain = _plain(model, pin)
    e_gov = _governed(model, pin)
    e_fan = _fan(model, pin)
    print(f"     plain={e_plain!r}  governed={e_gov!r}  fan={e_fan!r}")
    ck(f"{model} '{pin}': plain path sends the expected effort '{expect}'", e_plain == expect)
    ck(f"{model} '{pin}': governed=True sends the SAME effort as plain (no path-dependent default)", e_gov == e_plain)
    ck(f"{model} '{pin}': bulk_delegate fan sends the SAME effort as plain (incident-2 divergence cannot recur)",
       e_fan == e_plain)

print(f"\n{'[FAIL]' if _fails else 'OK'} test_effort_path_independent: {len(_fails)} failure(s)")
sys.exit(1 if _fails else 0)

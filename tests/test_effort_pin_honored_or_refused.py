"""Guardrail A — HONOR OR REFUSE AN EXPLICIT EFFORT PIN, NEVER DROP IT SILENTLY. A caller's `reasoning='minimal'` is a
COST pin. On a model whose verified floor IS 'minimal' (gpt-5-mini / gpt-5-nano / o-series) it must reach the wire as
'minimal' — HONORED. On gpt-5.x, whose floor is 'none' AND which STILL reasons at 'none' (reasons_by_default), the pin
cannot be honored — it must be SURFACED as a loud, un-swallowable refusal and recorded, never a silent 'none' that reads
as governed while it burns thousands of reasoning tokens (the $45 warden overspend: gpt-5.5 'none' → ~4,249 out tok).

Faithful end-to-end: drives the REAL effort resolution in adapters._call_once through a fake OpenAI client and reads
BOTH the reasoning_effort that reached the wire AND bulkgate.unhonored_efforts(). Acceptance test from
docs/GUARDRAILS_reasoning_overspend.md §1. Offline (no network)."""
import os
import sys
import tempfile
import types

os.environ.setdefault("SPENDGUARD_TEST_ISOLATED", "1")
os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="spendguard-effortpin-")
os.environ["SPENDGUARD_DISPATCH_XP_OFF"] = "1"

import openai  # noqa: E402
from spendguard import adapters, bulkgate, config, dispatch, models, vendor_call  # noqa: E402

_fails = []
def ck(label, cond):
    if not cond:
        _fails.append(label)
    print(f"  [{'OK' if cond else 'FAIL'}] {label}")

_COMPLETION = types.SimpleNamespace(
    choices=[types.SimpleNamespace(message=types.SimpleNamespace(content="ok"), finish_reason="stop")],
    usage=types.SimpleNamespace(prompt_tokens=5, completion_tokens=3))

_WIRE = []   # every kwargs dict that reached the vendor create() — so we can read the reasoning_effort actually SENT


class _CaptureClient:
    """A client whose chat.completions.create captures the wire kwargs and returns a fixed completion. with_raw_response
    is present (the modern SDK path adapters uses) and also captures, so the reasoning_effort we assert is the one that
    truly reached the request builder — not a re-derivation."""
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
        return types.SimpleNamespace(completions=types.SimpleNamespace(
            create=self._capture, with_raw_response=wr))

    def close(self):
        pass


config.api_key = lambda name: "sk-fake"                    # truthy key so _call_once reaches the client
vendor_call.served_check = lambda prov, raw: "unchecked"   # skip the live-catalog preflight (no network)
openai.OpenAI = lambda **kw: _CaptureClient()


def _wire_effort():
    """The reasoning_effort on the LAST wire call (None if none was sent)."""
    return (_WIRE[-1] or {}).get("reasoning_effort") if _WIRE else None


# ── ground the facts the guardrail reads (real models.py, not mocks) — the condition must discriminate on TRUTH ──
print("-- grounding: normalize_reasoning + reasons_by_default (the two facts guardrail A keys on) --")
ck("gpt-5.5 floor remaps 'minimal' → 'none' (cannot honor minimal)", models.normalize_reasoning("gpt-5.5", "minimal") == "none")
ck("gpt-5.5 STILL reasons at its floor (so 'none' is not cheap)", models.reasons_by_default("gpt-5.5") is True)
ck("gpt-5-nano HONORS 'minimal' (floor is 'minimal')", models.normalize_reasoning("gpt-5-nano", "minimal") == "minimal")

# ── (1) HONORED: a model whose floor is 'minimal' → 'minimal' reaches the wire, nothing flagged ──
print("-- (1) gpt-5-nano + reasoning='minimal' → HONORED on the wire, NOT flagged --")
_before = dict(bulkgate.unhonored_efforts())
r = adapters._call_once("openai:gpt-5-nano", "hi", max_tokens=100, reasoning="minimal", timeout_s=30)
ck("call succeeds", r.get("text") == "ok" and not r.get("error"))
ck("reasoning_effort='minimal' reached the wire (honored, not silently dropped)", _wire_effort() == "minimal")
ck("gpt-5-nano NOT recorded as unhonored (an honored pin must never false-alarm)",
   not any("gpt-5-nano" in k for k in bulkgate.unhonored_efforts() if k not in _before))

# ── (2) REFUSED-LOUDLY: gpt-5.5 can't honor 'minimal' → floor 'none' on the wire BUT recorded + surfaced ──
print("-- (2) gpt-5.5 + reasoning='minimal' → floor 'none' on wire, but LOUDLY recorded (the $45 incident path) --")
r2 = adapters._call_once("openai:gpt-5.5", "hi", max_tokens=100, reasoning="minimal", timeout_s=30)
ck("call still succeeds (we surface, we do not break the call)", r2.get("text") == "ok" and not r2.get("error"))
ck("the wire effort is the floor 'none' (minimal 400s on gpt-5.5), NOT a silent honored-minimal", _wire_effort() == "none")
_ue = bulkgate.unhonored_efforts()
ck("the non-honored pin is RECORDED for gpt-5.5 (loud refusal, not silent 'none' with the pin set)",
   any("gpt-5.5" in k and "minimal->none" in k for k in _ue))
ck("the recorded count is >= 1", any(v >= 1 for k, v in _ue.items() if "gpt-5.5" in k))

# ── (3) a NON-minimal pin is a real API tier, not a cost pin → passes through unchanged, never flagged ──
print("-- (3) gpt-5.5 + reasoning='high' → passthrough on the wire, NOT flagged (only 'minimal' is the cost pin) --")
_hi_before = {k for k in bulkgate.unhonored_efforts()}
r3 = adapters._call_once("openai:gpt-5.5", "hi", max_tokens=100, reasoning="high", timeout_s=30)
ck("reasoning_effort='high' reached the wire unchanged", _wire_effort() == "high")
ck("a high/low/medium tier is never flagged as unhonored (it is the API's own value)",
   {k for k in bulkgate.unhonored_efforts() if "->high" in k} == set())

# ── (4) OBSERVABILITY PARITY: the counter is queryable via the shared admission snapshot (CLI + MCP render it) ──
print("-- (4) admission_state() surfaces unhonored_efforts (parity: same data to CLI `dispatch` and MCP) --")
st = dispatch.admission_state()
ck("admission_state carries an 'unhonored_efforts' map", isinstance(st.get("unhonored_efforts"), dict))
ck("and it reflects the gpt-5.5 non-honor (auditable, not just printed)",
   any("gpt-5.5" in k for k in (st.get("unhonored_efforts") or {})))

print(f"\n{'[FAIL]' if _fails else 'OK'} test_effort_pin_honored_or_refused: {len(_fails)} failure(s)")
sys.exit(1 if _fails else 0)

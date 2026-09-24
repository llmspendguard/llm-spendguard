"""Guardrail E — PER-CALL RUNAWAY BREAKER. A completed call whose out_tok is many times the MEASURED p99 norm for its
class is a runaway the deliberately-loose reasoning output ceiling never truncates (the gpt-5.5 incident: ~4,249 out tok
where the class norm was ~121, billed in full x N). The breaker TRIPS and RECORDS it (visible), keyed to the measured
norm — it never aborts (a cut reasoning call still bills for nothing) and never accuses without a trustworthy norm.
Acceptance test from docs/GUARDRAILS_reasoning_overspend.md §5. Offline (no network).

Two layers: (1) the DECISION (check_runaway) on REAL seeded measurements — trips on a runaway, stays silent on a normal
call and on a cold class; (2) the WIRING — a real adapters.call whose completion runs away is recorded via _call_guarded.
"""
import os
import sys
import tempfile
import types

os.environ.setdefault("SPENDGUARD_TEST_ISOLATED", "1")
os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="spendguard-runaway-")
os.environ["SPENDGUARD_DISPATCH_XP_OFF"] = "1"
os.environ["SPENDGUARD_ROUTE_THROUGH_QUEUE"] = "0"

import openai  # noqa: E402
from spendguard import adapters, bulkgate, config, dispatch, vendor_call  # noqa: E402

_fails = []
def ck(label, cond):
    if not cond:
        _fails.append(label)
    print(f"  [{'OK' if cond else 'FAIL'}] {label}")

_MODEL = "openai:gpt-5.5"
_NORM = 120        # the measured normal output size for this class
_RUNAWAY = 4249    # the incident's runaway out_tok (~35x the norm)


def _seed_norm(sig_key, n=25, out_tok=_NORM):
    """Record n normal completed outputs so the class has a TRUSTWORTHY p99 (>= RUNAWAY_MIN_SAMPLES)."""
    for _ in range(n):
        bulkgate.note_response(sig_key, _MODEL, out_tok, max_tokens=8000, finish_reason="stop")


# ── (1) the DECISION on real measured norms ──
print("-- (1) check_runaway trips on a runaway, stays silent on a normal call, and on a cold class --")
_sig_warm = bulkgate.sig(_MODEL, template_id="runaway_decision")
ck("a COLD class (no measurements) does NOT trip — honest 'cannot judge yet', never a guessed absolute",
   bulkgate.check_runaway(_sig_warm, _MODEL, _RUNAWAY)[0] is False)
_seed_norm(_sig_warm)
_mx = bulkgate.maxtokens(_sig_warm)
ck("the class now has a trustworthy norm (n>=20, p99≈norm)", (_mx.get("n") or 0) >= 20 and _mx.get("p99"))
trip, detail = bulkgate.check_runaway(_sig_warm, _MODEL, _RUNAWAY)
ck("a call at ~35x the p99 TRIPS the breaker", trip is True and detail and detail["basis"] == "class")
ck("and it is RECORDED in runaways() (visible, not just returned)",
   any(_MODEL in k and _sig_warm in k for k in bulkgate.runaways()))
ck("a NORMAL call (just above the norm, below the factor) does NOT trip",
   bulkgate.check_runaway(_sig_warm, _MODEL, int(_NORM * 1.5))[0] is False)

# ── (2) the WIRING: a real adapters.call whose completion runs away is recorded through _call_guarded ──
print("-- (2) a real adapters.call that runs away is recorded (the incident path, end-to-end) --")
_COMPLETION = types.SimpleNamespace(
    choices=[types.SimpleNamespace(message=types.SimpleNamespace(content="ok"), finish_reason="stop")],
    usage=types.SimpleNamespace(prompt_tokens=5, completion_tokens=_RUNAWAY))   # the model RAN AWAY: 4,249 out tokens


class _RunawayClient:
    def __init__(self, **_kw):
        pass

    def _create(self, **_kw):
        return _COMPLETION

    @property
    def chat(self):
        return types.SimpleNamespace(completions=types.SimpleNamespace(create=self._create))

    def close(self):
        pass


config.api_key = lambda name: "sk-fake"
vendor_call.served_check = lambda prov, raw: "unchecked"
openai.OpenAI = lambda **kw: _RunawayClient()
dispatch.admit = lambda *a, **k: types.SimpleNamespace(ok=True, shed=False, error=None, release=lambda: None)
dispatch.acquire_or_none = lambda *a, **k: object()
dispatch.release = lambda *a, **k: None

_intent = "runaway_wiring"
_sig_e2e = bulkgate.sig(_MODEL, template_id=_intent)   # the SAME key _call_guarded derives from (model, intent)
_seed_norm(_sig_e2e)                                    # give the class a norm so the runaway can be judged
_before = sum(bulkgate.runaways().values())
r = adapters.call(_MODEL, "classify this", intent=_intent, reasoning="minimal", timeout_s=30)
ck("the call returned the (runaway) completion", isinstance(r, dict) and (r.get("out_tok") == _RUNAWAY))
ck("the runaway was RECORDED through _call_guarded's wiring (billed in full, but now VISIBLE)",
   sum(bulkgate.runaways().values()) > _before)

# ── (3) OBSERVABILITY PARITY: the breaker is queryable via the shared admission snapshot (CLI + MCP render it) ──
print("-- (3) admission_state() surfaces runaways (parity: same data to CLI `dispatch` and MCP) --")
st = dispatch.admission_state()
ck("admission_state carries a 'runaways' map", isinstance(st.get("runaways"), dict))
ck("and it reflects the recorded runaways (auditable, not just printed)", bool(st.get("runaways")))

print(f"\n{'[FAIL]' if _fails else 'OK'} test_runaway_breaker: {len(_fails)} failure(s)")
sys.exit(1 if _fails else 0)

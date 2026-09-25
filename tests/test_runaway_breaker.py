"""Guardrail E — PER-CALL RUNAWAY BREAKER, now OUTPUT-CLASS/INTENT-AWARE. A completed call whose out_tok is many times
its CALL-CLASS's OWN norm is a runaway the deliberately-loose reasoning output ceiling never truncates (the gpt-5.5
incident: ~4,249 out tok where the class norm was ~121, billed in full x N). The breaker TRIPS and RECORDS it (visible),
keyed to the CLASS norm — never the model-wide average across all call-classes, because output size is a property of the
JOB not the model: a norm built from a model's SMALL-output classes false-accuses its LARGE-output ones (MEASURED — a
legitimate multi-vendor panel review tripped every reviewer >3x a ~268-tok model p99 while each review's own class norm
is thousands). It never aborts (a cut reasoning call still bills for nothing) and never accuses without a trustworthy
CLASS norm. Acceptance test from docs/GUARDRAILS_reasoning_overspend.md §5. Offline (no network).

Layers: (1) the DECISION (check_runaway) on REAL seeded measurements — warm-class p99, cold-class reasoning seed, and
the REGRESSION that the model-wide fallback is GONE; (2) the WIRING — a real adapters.call whose completion runs away is
recorded via _call_guarded; (3) OBSERVABILITY PARITY via the admission snapshot.
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

_MODEL_R = "openai:gpt-5.5"                 # reasons_by_default → a COLD class gets a reasoning-inclusive seed norm
_MODEL_NR = "anthropic:claude-opus-4-8"    # non-reasoning → a COLD class has NO seed (honest 'cannot judge yet')
_NORM = 120        # the measured normal output size for a warm class
_RUNAWAY = 4249    # the incident's runaway out_tok (~35x the warm norm)
_SEED = bulkgate._reasoning_out_estimate()          # the cold-class reasoning-inclusive norm (env→config→default)
_FACTOR = bulkgate._runaway_factor()                # the trip multiple (out_tok > factor x norm)


def _seed_norm(sig_key, model, n=25, out_tok=_NORM):
    """Record n normal completed outputs so the class has a TRUSTWORTHY p99 (>= RUNAWAY_MIN_SAMPLES)."""
    for _ in range(n):
        bulkgate.note_response(sig_key, model, out_tok, max_tokens=8000, finish_reason="stop")


# ── (1a) COLD reasoning class → judged against its OWN reasoning-inclusive seed, never the model-wide average ──
print("-- (1a) cold reasoning class: judged vs its own seed --")
_sig_cold_r = bulkgate.sig(_MODEL_R, template_id="cold_reasoning")
ck("a NORMAL-sized first output (just under factor x seed) does NOT trip",
   bulkgate.check_runaway(_sig_cold_r, _MODEL_R, int(_SEED * _FACTOR) - 1)[0] is False)
_trip, _detail = bulkgate.check_runaway(_sig_cold_r, _MODEL_R, int(_SEED * _FACTOR) + 5000)
ck("an EGREGIOUS first output (>> factor x seed) trips, basis 'class-seed(cold)'",
   _trip is True and _detail and _detail["basis"] == "class-seed(cold)")

# ── (1b) COLD non-reasoning class → no seed → cannot judge → never trips (not even on a huge output) ──
print("-- (1b) cold non-reasoning class: no seed → cannot judge → never false-accuses --")
_sig_cold_nr = bulkgate.sig(_MODEL_NR, template_id="cold_nonreasoning")
ck("a huge first output on a cold NON-reasoning class does NOT trip (honest 'cannot judge yet')",
   bulkgate.check_runaway(_sig_cold_nr, _MODEL_NR, 100_000)[0] is False)

# ── (1c) WARM class → the incident: trips at ~35x its measured p99, records, silent on a normal call ──
print("-- (1c) warm class: the gpt-5.5 incident --")
_sig_warm = bulkgate.sig(_MODEL_R, template_id="warm_incident")
ck("cold (pre-seed) reasoning class does not trip at the incident size (below factor x seed)",
   bulkgate.check_runaway(_sig_warm, _MODEL_R, _RUNAWAY)[0] is False)
_seed_norm(_sig_warm, _MODEL_R)
_mx = bulkgate.maxtokens(_sig_warm)
ck("the class now has a trustworthy norm (n>=20, p99≈norm)", (_mx.get("n") or 0) >= 20 and _mx.get("p99"))
_trip, _detail = bulkgate.check_runaway(_sig_warm, _MODEL_R, _RUNAWAY)
ck("a call at ~35x the p99 TRIPS the breaker, basis 'class'", _trip is True and _detail and _detail["basis"] == "class")
ck("and it is RECORDED in runaways() (visible, not just returned)",
   any(_MODEL_R in k and _sig_warm in k for k in bulkgate.runaways()))
ck("a NORMAL call (just above the norm, below the factor) does NOT trip",
   bulkgate.check_runaway(_sig_warm, _MODEL_R, int(_NORM * 1.5))[0] is False)

# ── (1d) THE REGRESSION — a COLD class does NOT borrow the model-wide p99 (the panel false-positive, locked out) ──
print("-- (1d) the model-wide cross-class fallback is REMOVED --")
_sig_other = bulkgate.sig(_MODEL_NR, template_id="other_small_class")
_seed_norm(_sig_other, _MODEL_NR)                    # the MODEL now has a warm, SMALL p99 across this other class
_mo = bulkgate.model_outputs(_MODEL_NR)
ck("the model-wide p99 is warm and small (what the OLD code would have used)", (_mo.get("n") or 0) >= 20 and _mo.get("p99"))
_sig_target_cold = bulkgate.sig(_MODEL_NR, template_id="target_cold_large_output")
ck("a COLD class with a large output does NOT trip against the model-wide p99 (the false-positive is gone)",
   bulkgate.check_runaway(_sig_target_cold, _MODEL_NR, _RUNAWAY)[0] is False)
ck("(sanity: the OLD model-wide fallback WOULD have tripped this — proving the fallback is truly removed)",
   bool(_mo.get("p99")) and _RUNAWAY > _FACTOR * _mo["p99"])

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
_sig_e2e = bulkgate.sig(_MODEL_R, template_id=_intent)   # the SAME key _call_guarded derives from (model, intent)
_seed_norm(_sig_e2e, _MODEL_R)                           # give the class a warm norm so the runaway can be judged
_before = sum(bulkgate.runaways().values())
r = adapters.call(_MODEL_R, "classify this", intent=_intent, reasoning="minimal", timeout_s=30)
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

"""Guardrail C — the ESTIMATE is reasoning-aware, on BOTH a warm and a COLD class. A reasoning model's real output is
reasoning+answer (thousands of tokens that bill as OUTPUT). expected_output.expect already returns the measured p90 for
a WARM class (reasoning-inclusive — completion_tokens counts reasoning); the gap was the COLD class: with no history it
fell through to a caller-cap / the 128k published ceiling / unknown, never a reasoning-aware number — the incident
(gpt-5.5 estimated at per_out=160, ~9x under). This adds a cold-reasoning-floor rung using the SAME seed maxtokens uses.
Acceptance test from docs/GUARDRAILS_reasoning_overspend.md §3. Offline (no network; expect() reads only the local DB).
"""
import os
import sys
import tempfile

os.environ.setdefault("SPENDGUARD_TEST_ISOLATED", "1")
os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="spendguard-coldfloor-")

from spendguard import bulkgate, expected_output, models  # noqa: E402

_fails = []
def ck(label, cond):
    if not cond:
        _fails.append(label)
    print(f"  [{'OK' if cond else 'FAIL'}] {label}")

_FLOOR = bulkgate._reasoning_out_estimate()   # the documented reasoning-inclusive seed (env → config → default)
ck("the reasoning seed is a real reasoning-scale number (>= 1000, NOT a ~160 visible-answer guess)", _FLOOR >= 1000)

# ── (1) WARM reasoning class: the estimate is the MEASURED p90, within a bounded factor of realized ──
print("-- (1) a WARM reasoning class estimates from measured p90 (reasoning-inclusive), within 2x of realized --")
_WARM_SIG = "coldfloor_warm"
_REALIZED = 2000                              # this class really emits ~2000 out tok/call (reasoning+answer)
for _ in range(25):
    bulkgate.note_response(_WARM_SIG, "openai:gpt-5-mini", _REALIZED, max_tokens=16000, finish_reason="stop")
_est_warm, _basis_warm = expected_output.expect("openai:gpt-5-mini", sig=_WARM_SIG)
print(f"     warm estimate={_est_warm} basis={_basis_warm!r} realized={_REALIZED}")
ck("the warm estimate is MEASURED (basis 'learned'), not a ceiling or a guess", _basis_warm == "learned")
ck("the warm estimate is within 2x of realized (reasoning tokens are counted, so it is not ~160)",
   _est_warm and 0.5 <= (_est_warm / _REALIZED) <= 2.0)

# ── (2) COLD reasoning class: no history AT ALL → the reasoning FLOOR, never naive-160 / ceiling / unknown ──
print("-- (2) a COLD reasoning class (no history) estimates from the reasoning FLOOR, not 160/ceiling/unknown --")
_est_cold, _basis_cold = expected_output.expect("openai:gpt-5.5", sig="coldfloor_cold_unused")
print(f"     cold estimate={_est_cold} basis={_basis_cold!r}")
ck("a cold gpt-5.5 (reasoning) estimate is the reasoning floor", _est_cold == _FLOOR and _basis_cold == "reasoning-floor")
ck("REGRESSION GUARD: the cold estimate is NOT the ~160 naive assumption (it is >> 160)", _est_cold > 500)
ck("and it is NOT the 128k published ceiling (a ceiling over-states ~100x)", _est_cold < 100000)
ck("and it is NOT 0/unknown (a silent zero is the bug this module exists to end)",
   _est_cold > 0 and _basis_cold != "unknown")

# ── (2b) an EXPLICIT caller cap is a HARD bound and WINS over the reasoning floor (batch enforces it) ──
print("-- (2b) a cold reasoning model WITH an explicit max_tokens uses the CAP, not the reasoning floor --")
_est_cap, _basis_cap = expected_output.expect("openai:gpt-5.5", sig="coldfloor_cold_capped", max_tokens=100)
print(f"     cold reasoning + max_tokens=100 → estimate={_est_cap} basis={_basis_cap!r}")
ck("an explicit caller cap wins (basis 'caller-cap', value = the cap) — the reasoning floor never overrides it",
   _est_cap == 100 and _basis_cap == "caller-cap")

# ── (3) the rung is REASONING-GATED: a cold NON-reasoning model never gets the reasoning floor ──
print("-- (3) a cold NON-reasoning model does NOT get the reasoning floor (the rung is gated on reasons_by_default) --")
_NONREASON = "openai:gpt-4o"
ck("ground: gpt-4o does NOT reason by default (so the rung must skip it)",
   models.reasons_by_default(_NONREASON) is False)
_est_nr, _basis_nr = expected_output.expect(_NONREASON, sig="coldfloor_cold_nonreason")
print(f"     cold non-reasoning estimate={_est_nr} basis={_basis_nr!r}")
ck("a cold non-reasoning model falls through PAST the reasoning floor (basis != 'reasoning-floor')",
   _basis_nr != "reasoning-floor")

print(f"\n{'[FAIL]' if _fails else 'OK'} test_reasoning_estimate_coldfloor: {len(_fails)} failure(s)")
sys.exit(1 if _fails else 0)

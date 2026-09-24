"""Guard — #1 reasoning-aware estimate SEED. Reasoning (thinking) tokens bill as OUTPUT, so an estimate sized from the
visible answer under-counts them badly (measured: a per_out=160 estimate came in ~9x low on gpt-5.5, $51.88 vs $13.69).
bulkgate.maxtokens(sig, model=...) therefore seeds an UNMEASURED reasoning model's output estimate with a
reasoning-inclusive floor (not None/naive-low) + a loud warn; a non-reasoning model / no model is unchanged; once real
calls are measured, the DATA replaces the seed. Env/config override honored. Hermetic: isolated home, no network."""
import os
import sys
import tempfile

os.environ.setdefault("SPENDGUARD_TEST_ISOLATED", "1")
os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="spendguard-rest-")

from spendguard import bulkgate, models

_fails = []
def ck(label, cond):
    if not cond:
        _fails.append(label)
    print(f"  [{'OK' if cond else 'FAIL'}] {label}")

SIG = "unmeasured:reasoning:class:xyz"     # no gate_calls rows → the no-data branch
FLOOR = bulkgate.REASONING_OUT_ESTIMATE

# sanity on the predicate the seed keys off
print("-- sanity: reasons_by_default distinguishes reasoning from non-reasoning models --")
ck("gpt-5.5 reasons_by_default (mandatory reasoning floor)", models.reasons_by_default("openai:gpt-5.5"))
ck("an embedding model does NOT reason", not models.reasons_by_default("openai:text-embedding-3-large"))

# (1) unmeasured REASONING model → reasoning-inclusive seed + a warn that names the under-count
print("-- (1) unmeasured reasoning class → recommend seeded (not None) + reasoning warn --")
r = bulkgate.maxtokens(SIG, model="openai:gpt-5.5")
ck("recommend is the reasoning-inclusive floor (not None)", r.get("recommend") == FLOOR)
_w = (r.get("warn") or "").lower()
ck("the warn names reasoning + that it bills as output", "reason" in _w and "output" in _w)

# (2) unmeasured NON-reasoning model → recommend None (unchanged behaviour)
print("-- (2) a non-reasoning model / no model → recommend None (unchanged) --")
ck("non-reasoning model → None", bulkgate.maxtokens(SIG, model="openai:text-embedding-3-large").get("recommend") is None)
ck("no model arg → None (backward compatible)", bulkgate.maxtokens(SIG).get("recommend") is None)

# (3) env override honored (parity: env → config → default)
print("-- (3) the seed is overridable via env (knob parity) --")
os.environ["SPENDGUARD_BULKGATE_REASONING_OUT_ESTIMATE"] = "8000"
try:
    ck("env override changes the seed to 8000", bulkgate.maxtokens(SIG, model="openai:gpt-5.5").get("recommend") == 8000)
finally:
    os.environ.pop("SPENDGUARD_BULKGATE_REASONING_OUT_ESTIMATE", None)

# (4) once MEASURED, the data replaces the seed (the seed is only for the unmeasured window)
print("-- (4) measured data replaces the seed (recommend comes from the observed p99, not the floor) --")
for _ in range(3):
    bulkgate.note_response(SIG, "openai:gpt-5.5", out_tok=5000, max_tokens=32000, finish_reason="stop")
r4 = bulkgate.maxtokens(SIG, model="openai:gpt-5.5")
ck("now measured (n>=1) and recommend is derived from the DATA, not the seed",
   r4.get("n") >= 1 and r4.get("recommend") not in (FLOOR, None) and r4.get("recommend") >= 5000)

print(f"\n{'[FAIL]' if _fails else 'OK'} test_reasoning_estimate_seed: {len(_fails)} failure(s)")
sys.exit(1 if _fails else 0)

"""gate.autotune — the SEND cap it emits is ALWAYS governed by the 32K output floor.

WHY THIS GUARD EXISTS. A max_tokens cap is billed on tokens GENERATED, so a high cap is free and the ONLY failure
mode is one set too LOW (it truncates a real answer for no saving). autotune used to SHRINK a "wasteful" cap down
to the class's measured VISIBLE-output p99 — which, for a reasoning model whose hidden reasoning is NOT in that
p99, collapsed the cap to single digits and truncated every reply (measured 2026-10-12: honestreview judges
clamped to a 7-token cap, honestreview:coding_router truncated 3,337/10,515). The floor (adapters.TOKEN_FLOOR /
pricing.OUTPUT_FLOOR — the SAME one output_budget sends on the metered path) is now the law in autotune too:

  * a cap BELOW min(32K, the model's published max) is RAISED to it — unconditionally, no observations needed;
  * a wasteful OVER-LARGE cap is pulled down ONLY to that floor, NEVER below it;
  * autotune still never ADDS a cap where the caller set none, still honors suggest/off/opt-out.

Offline (stubbed SDK), zero spend.
"""
import os
import sys
import tempfile

if not os.environ.get("SPENDGUARD_TEST_ISOLATED"):
    os.environ["SPENDGUARD_TEST_ISOLATED"] = "1"
    os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="spendguard-autotune-")
    os.execv(sys.executable, [sys.executable] + sys.argv)

import types
from spendguard import gate as spend_gate
from spendguard import adapters, bulkgate, calls, pricing

fails = []


def ck(name, cond):
    print(("  [OK] " if cond else "  [FAIL] ") + name)
    if not cond:
        fails.append(name)


MODEL = "gpt-5.5"
INTENT = "edge_cards"
SIG = bulkgate.sig(MODEL, template_id=INTENT)
for _ in range(40):                                     # measured clean history: small visible outputs ≈ 300
    bulkgate.note_response(SIG, MODEL, 300, max_tokens=40000, finish_reason="stop")

# The floor is resolved the SAME way _autotune resolves it (no hardcoded 32000): 32K, clamped DOWN to the model's
# own published max when that is lower. For gpt-5.5 (published max >= 32K) this is 32000.
_pub = int(pricing.max_output_tokens(MODEL) or 0)
FLOOR = min(int(adapters.TOKEN_FLOOR), _pub) if _pub else int(adapters.TOKEN_FLOOR)
BIG = FLOOR * 5                                          # an over-large wasteful cap, safely above the floor

logged = []
orig_log = spend_gate._log
spend_gate._log = lambda r: logged.append(r)

os.environ["SPENDGUARD_AUTOTUNE"] = "suggest"
with calls.context(intent=INTENT):
    kw = dict(model=MODEL, max_tokens=4000)
    spend_gate._autotune(kw, MODEL)
ck("suggest NEVER mutates the call", kw["max_tokens"] == 4000)

os.environ["SPENDGUARD_AUTOTUNE"] = "apply"
with calls.context(intent=INTENT):
    kw = dict(model=MODEL, max_tokens=4000)             # a sub-floor cap: the money-losing setting
    spend_gate._autotune(kw, MODEL)
ck(f"a sub-floor cap is RAISED to the output floor ({FLOOR}), not left to truncate", kw["max_tokens"] == FLOOR)
ck("the floor-raise is LOGGED with direction=floor + to",
   logged and logged[-1]["kind"] == "autotune" and logged[-1]["direction"] == "floor"
   and logged[-1]["to"] == FLOOR)

with calls.context(intent=INTENT):
    kw = dict(model=MODEL, max_tokens=FLOOR)            # already at the floor
    spend_gate._autotune(kw, MODEL)
    ck("a cap already at the floor is untouched (never shrunk BELOW it)", kw["max_tokens"] == FLOOR)

    kw = dict(model=MODEL, max_tokens=BIG)              # wasteful over-large cap, clean history
    spend_gate._autotune(kw, MODEL)
    ck("a wasteful over-large cap is pulled down ONLY to the floor, never below", kw["max_tokens"] == FLOOR)

    kw = dict(model=MODEL)                              # caller set no cap
    spend_gate._autotune(kw, MODEL)
    ck("NEVER adds a cap where the caller set none", "max_tokens" not in kw)

    kw = dict(model=MODEL, max_tokens=4000, autotune=False)
    spend_gate._autotune(kw, MODEL)
    ck("per-call opt-out (autotune=False) is honored + stripped", kw["max_tokens"] == 4000 and "autotune" not in kw)

# thin history → no MEASURED shrink (a cap already at/above the floor is left alone)
SIG2 = bulkgate.sig(MODEL, template_id="thin_class")
for _ in range(5):
    bulkgate.note_response(SIG2, MODEL, 300, max_tokens=40000, finish_reason="stop")
with calls.context(intent="thin_class"):
    kw = dict(model=MODEL, max_tokens=BIG)
    spend_gate._autotune(kw, MODEL)
ck("<30 observations → no measured shrink (cap above the floor left alone)", kw["max_tokens"] == BIG)

# a class that has truncated is never clamped DOWN (shrink vetoed; a raise never lowers)
bulkgate.note_response(SIG, MODEL, 40000, max_tokens=40000, finish_reason="length")
with calls.context(intent=INTENT):
    kw = dict(model=MODEL, max_tokens=BIG)
    spend_gate._autotune(kw, MODEL)
ck("a class that has truncated is never clamped DOWN", kw["max_tokens"] == BIG)

os.environ["SPENDGUARD_AUTOTUNE"] = "off"
SIG3 = bulkgate.sig(MODEL, template_id="clean2")
for _ in range(40):
    bulkgate.note_response(SIG3, MODEL, 300, max_tokens=40000, finish_reason="stop")
with calls.context(intent="clean2"):
    kw = dict(model=MODEL, max_tokens=4000)
    spend_gate._autotune(kw, MODEL)
ck("off → untouched", kw["max_tokens"] == 4000)
spend_gate._log = orig_log

# end-to-end: the wrapper path actually delivers the FLOORED cap to the SDK call
os.environ["SPENDGUARD_AUTOTUNE"] = "apply"
seen = []
from openai.resources.chat import completions as _oc
_oc.Completions.create = lambda self, *a, **k: seen.append(k.get("max_tokens")) or types.SimpleNamespace(
    model=MODEL, usage=types.SimpleNamespace(prompt_tokens=10, completion_tokens=10))
spend_gate.install()
import openai
client = openai.OpenAI(api_key="test-key-not-real")
os.environ["GATE_RT_BUDGET"] = "1000"
with calls.context(intent="clean2"):
    client.chat.completions.create(model=MODEL, messages=[{"role": "user", "content": "hi"}], max_tokens=4000)
ck("end-to-end: the SDK receives the FLOORED max_tokens (a sub-floor caller cap never reaches the provider)",
   seen and seen[0] == FLOOR)

# honest accounting: autotune records NO counterfactual saving
import inspect
src = inspect.getsource(spend_gate._autotune)
ck("no counterfactual 'saving' recorded by autotune", "record_saving" not in src)

print(("[OK]" if not fails else "[FAIL]") + " gate autotune: %d failure(s)" % len(fails))
sys.exit(1 if fails else 0)

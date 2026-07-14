"""gate.autotune — measured max_tokens becomes a default you can't forget, SAFELY:
only SHRINKS an existing wasteful cap (never raises, never adds), only with ≥30 observations and a
clean truncation record, per-call opt-out, one truncation after a clamp permanently vetoes the class
(the recorded truncation counter IS the backoff state), and no counterfactual 'saving' is ever
recorded. Offline (stubbed SDK), zero spend."""
import os
import sys
import tempfile

if not os.environ.get("SPENDGUARD_TEST_ISOLATED"):
    os.environ["SPENDGUARD_TEST_ISOLATED"] = "1"
    os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="spendguard-autotune-")
    os.execv(sys.executable, [sys.executable] + sys.argv)

import types
from spendguard import gate as spend_gate
from spendguard import bulkgate, calls

fails = []


def ck(name, cond):
    print(("  [OK] " if cond else "  [FAIL] ") + name)
    if not cond:
        fails.append(name)


MODEL = "gpt-5.5"
INTENT = "edge_cards"
SIG = bulkgate.sig(MODEL, template_id=INTENT)
for _ in range(40):                                     # measured clean history: outputs ≈ 300, cap 4000
    bulkgate.note_response(SIG, MODEL, 300, max_tokens=4000, finish_reason="stop")
rec = int(bulkgate.maxtokens(SIG, 4000)["recommend"])   # p99×1.5 ≈ 450

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
    kw = dict(model=MODEL, max_tokens=4000)
    spend_gate._autotune(kw, MODEL)
ck(f"apply clamps a wasteful cap to the measured bound ({rec})", kw["max_tokens"] == rec)
ck("every application is LOGGED with from/to/n_obs",
   logged and logged[-1]["kind"] == "autotune" and logged[-1]["from"] == 4000
   and logged[-1]["to"] == rec and logged[-1]["n_obs"] == 40)

with calls.context(intent=INTENT):
    kw = dict(model=MODEL, max_tokens=rec)              # caller already at the bound
    spend_gate._autotune(kw, MODEL)
    ck("a sane cap is untouched (slack respected)", kw["max_tokens"] == rec)
    kw = dict(model=MODEL, max_tokens=100)              # caller BELOW the bound
    spend_gate._autotune(kw, MODEL)
    ck("NEVER raises a cap", kw["max_tokens"] == 100)
    kw = dict(model=MODEL)                              # caller set no cap
    spend_gate._autotune(kw, MODEL)
    ck("NEVER adds a cap where the caller set none", "max_tokens" not in kw)
    kw = dict(model=MODEL, max_tokens=4000, autotune=False)
    spend_gate._autotune(kw, MODEL)
    ck("per-call opt-out (autotune=False) is honored + stripped", kw["max_tokens"] == 4000 and "autotune" not in kw)

# thin history → no action
SIG2 = bulkgate.sig(MODEL, template_id="thin_class")
for _ in range(5):
    bulkgate.note_response(SIG2, MODEL, 300, max_tokens=4000, finish_reason="stop")
with calls.context(intent="thin_class"):
    kw = dict(model=MODEL, max_tokens=4000)
    spend_gate._autotune(kw, MODEL)
ck("<30 observations → vetoed (no clamp)", kw["max_tokens"] == 4000)

# ONE truncation permanently vetoes the class — the self-healing backoff
bulkgate.note_response(SIG, MODEL, rec, max_tokens=rec, finish_reason="length")
with calls.context(intent=INTENT):
    kw = dict(model=MODEL, max_tokens=4000)
    spend_gate._autotune(kw, MODEL)
ck("a truncation on the class permanently vetoes further clamps (backoff via the recorded counter)",
   kw["max_tokens"] == 4000)

os.environ["SPENDGUARD_AUTOTUNE"] = "off"
SIG3 = bulkgate.sig(MODEL, template_id="clean2")
for _ in range(40):
    bulkgate.note_response(SIG3, MODEL, 300, max_tokens=4000, finish_reason="stop")
with calls.context(intent="clean2"):
    kw = dict(model=MODEL, max_tokens=4000)
    spend_gate._autotune(kw, MODEL)
ck("off → untouched", kw["max_tokens"] == 4000)
spend_gate._log = orig_log

# end-to-end: the wrapper path actually delivers the clamped kw to the SDK call
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
ck("end-to-end: the SDK receives the CLAMPED max_tokens", seen and seen[0] == int(bulkgate.maxtokens(SIG3, 4000)["recommend"]))

# honest accounting: autotune records NO counterfactual saving
import inspect
src = inspect.getsource(spend_gate._autotune)
ck("no counterfactual 'saving' recorded by autotune", "record_saving" not in src)

print(("[OK]" if not fails else "[FAIL]") + " gate autotune: %d failure(s)" % len(fails))
sys.exit(1 if fails else 0)

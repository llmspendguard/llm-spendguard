"""Non-token surfaces (images / transcription / TTS / fine-tune jobs) — captured, budget-enforced,
never guessed. Unit prices are INJECTED into pricing.UNIT_PRICES (the table ships empty until curated
entries or `sync-prices` provide sourced numbers); an unpriced unit records the call at $0 with a loud
warn. Fine-tune jobs are recorded as UNESTIMATED submissions (their $ lands at reconcile). Offline
(stubbed SDK methods), zero spend."""
import os
import sys
import tempfile

if not os.environ.get("SPENDGUARD_TEST_ISOLATED"):
    os.environ["SPENDGUARD_TEST_ISOLATED"] = "1"
    os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="spendguard-units-")
    os.execv(sys.executable, [sys.executable] + sys.argv)

import types
from spendguard import gate as spend_gate
from spendguard import pricing

fails = []


def ck(name, cond):
    print(("  [OK] " if cond else "  [FAIL] ") + name)
    if not cond:
        fails.append(name)


# inject unit prices (values arbitrary for math checks — the SHIPPED table stays sourced-only)
pricing.UNIT_PRICES["image"]["img-model:1024x1024:hd"] = 0.08
pricing.UNIT_PRICES["audio_second"]["stt-model"] = 0.0001
pricing.UNIT_PRICES["tts_char"]["tts-model"] = 0.000015

# ── estimators ──
m, usd = spend_gate._est_usd_images(dict(model="img-model", n=3, size="1024x1024", quality="hd"))
ck("images est = n × per-image (3 × $0.08)", m == "img-model" and abs(usd - 0.24) < 1e-9)
m, usd = spend_gate._est_usd_images(dict(model="unpriced-img", n=2))
ck("unpriced image est = $0 (fail-open, never guessed)", usd == 0.0)
m, usd = spend_gate._est_usd_speech(dict(model="tts-model", input="x" * 2000))
ck("tts est = chars × per-char (2000 × $0.000015)", abs(usd - 0.03) < 1e-9)

# ── actuals recording (via the real _record_rt seam, captured) ──
recorded = []
orig_record = spend_gate._record_rt
spend_gate._record_rt = lambda model, kw, i, o, **k: recorded.append(
    (model, i, o, round(k["cost"], 6) if k.get("cost") is not None else -1))

spend_gate._act_images(dict(model="img-model", size="1024x1024", quality="hd"),
                       types.SimpleNamespace(data=[1, 2]))
ck("images actual = len(data) × per-image ($0.16)", recorded[-1] == ("img-model", 0, 0, 0.16))

spend_gate._act_transcription(dict(model="gpt-4o-transcribe"),
                              types.SimpleNamespace(usage=types.SimpleNamespace(input_tokens=500, output_tokens=80)))
ck("token-billing transcribe records TOKENS (usage path)", recorded[-1] == ("gpt-4o-transcribe", 500, 80, -1))

spend_gate._act_transcription(dict(model="stt-model"), types.SimpleNamespace(duration=120.0, usage=None))
ck("whisper-style transcribe records duration × per-second ($0.012)", recorded[-1] == ("stt-model", 0, 0, 0.012))

spend_gate._act_transcription(dict(model="stt-unpriced"), types.SimpleNamespace(usage=None))
ck("no usage + no duration → recorded at $0, loudly (never invisible)", recorded[-1] == ("stt-unpriced", 0, 0, 0.0))

spend_gate._act_speech(dict(model="tts-model", input="y" * 1000), object())
ck("tts actual = chars × per-char ($0.015)", recorded[-1] == ("tts-model", 0, 0, 0.015))

spend_gate._record_rt = orig_record

# ── fine-tune job: unestimated submission is LOGGED, never silent ──
logged = []
orig_log = spend_gate._log
spend_gate._log = lambda rec: logged.append(rec)
spend_gate._act_finetune(dict(model="gpt-4o-mini"), types.SimpleNamespace(id="ftjob-1"))
spend_gate._log = orig_log
ck("fine-tune submission logged with job id + decision",
   logged and logged[-1]["kind"] == "finetune_job" and logged[-1]["job"] == "ftjob-1"
   and logged[-1]["decision"] == "unestimated_submission")

# ── the surfaces are registered + actually patched + budget-enforced end to end ──
regs = {(s[0], s[2]) for s in spend_gate.UNIT_INTERCEPTORS}
ck("all four unit surfaces registered (sync+async)",
   {("openai.resources.images", "generate"), ("openai.resources.audio.transcriptions", "create"),
    ("openai.resources.audio.speech", "create"), ("openai.resources.fine_tuning.jobs", "create")} <= regs
   and len(spend_gate.UNIT_INTERCEPTORS) == 8)

from openai.resources import images as _oi
_oi.Images.generate = lambda self, *a, **k: types.SimpleNamespace(data=[1, 2, 3])
spend_gate.install()
ck("images.generate IS gated after install()", getattr(_oi.Images.generate, "_spend_gated", False) is True)

import openai
client = openai.OpenAI(api_key="test-key-not-real")
for k in ("GATE_ALLOW", "GATE_DISABLE"):
    os.environ.pop(k, None)
os.environ["GATE_RT_BUDGET"] = "1000"
spend_gate._rt_spent = 0.0
client.images.generate(model="img-model", n=3, size="1024x1024", quality="hd")
ck("a priced image call is ACCOUNTED into the realtime tally ($0.24 actual for 3 images)",
   abs(spend_gate._rt_spent - 0.24) < 1e-9)

os.environ["GATE_RT_BUDGET"] = "0.10"                 # below what's already spent → next unit call refused
refused = False
try:
    client.images.generate(model="img-model", n=1, size="1024x1024", quality="hd")
except spend_gate.SpendGateRefused:
    refused = True
ck("over the realtime budget, an IMAGE call is REFUSED like any token call", refused)
os.environ["GATE_RT_BUDGET"] = "1000"

# ── rails: the shipped unit table carries no invented numbers ──
import json
shipped = json.load(open(os.path.join(os.path.dirname(spend_gate.__file__), "prices.json")))
ck("shipped prices.json has no unsourced unit_prices (empty or absent until verified entries land)",
   not shipped.get("unit_prices"))

print(("[OK]" if not fails else "[FAIL]") + " gate units: %d failure(s)" % len(fails))
sys.exit(1 if fails else 0)

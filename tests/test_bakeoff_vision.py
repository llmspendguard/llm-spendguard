"""GUARD — spendguard bakeoff supports IMAGE (vision) tasks, so a vision intent (7thsense-vision-caption) earns
quality labels the same way a text intent does. A task is EITHER a plain STRING (text, unchanged) OR
{"prompt": str, "images": [ref, …]} (vision). Pins the three correctness claims from the spec:
  (a) a vision task's ESTIMATE includes the IMAGE input-token cost — via the reused adapters._image_input_tokens
      (the provider-aware corrected estimator), never a hardcoded per-image number — so estimate-first stays
      accurate for a vision slate;
  (b) the run loop AND BOTH judge tiers RECEIVE images= for an image task (generic _judge_one; requirement-aware
      SCREEN + ADJUDICATOR) — a judge that scored a caption without seeing the image would be measuring nothing —
      while requirement EXTRACTION (criteria from the prompt text) does NOT get the image;
  (c) a plain-STRING bakeoff is byte-for-byte the prior behaviour: its candidate call passes NO images kwarg.
Hermetic: adapters.call, the image-token estimator, and the judge results are stubbed; a REAL 1x1 PNG data-url so
_load_image runs; isolated home; no network, no spend.
"""
import os
import sys
import tempfile

os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="sg-bakeoff-vision-")
os.environ.setdefault("SPENDGUARD_TEST_ISOLATED", "1")
os.environ.setdefault("SPENDGUARD_NO_AUTOINSTALL", "1")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from spendguard import bakeoff, adapters   # noqa: E402

fails = []


def ck(name, cond):
    print(("  [OK] " if cond else "  [FAIL] ") + name)
    if not cond:
        fails.append(name)


# a real, tiny 1x1 PNG as a data URL — adapters._load_image parses it (reads dims), so the vision path runs for real
PNG = ("data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==")
INTENT = "7thsense-vision-caption"
CANDS = ["openai:gpt-5-mini"]
TASK_TXT = "Describe the scene in one sentence."
VISION_TASK = {"prompt": TASK_TXT, "images": [PNG]}

# ── (a) the vision ESTIMATE includes image tokens — stub the estimator to a known count (tests the WIRING; the
#        estimator's own math is tested elsewhere) and compare against the identical text task ──
_IMG_TOK = {"n": 0}
adapters._image_input_tokens = lambda loaded, provider, model: _IMG_TOK["n"]

print("-- (a) the vision estimate includes the image input-token cost --")
_IMG_TOK["n"] = 0
est_text = bakeoff.bakeoff(INTENT, candidates=CANDS, prompts=[TASK_TXT], run=False)["estimate_usd"]
est_v0 = bakeoff.bakeoff(INTENT, candidates=CANDS, prompts=[VISION_TASK], run=False)["estimate_usd"]
ck("with 0 image tokens, a vision task prices EXACTLY like the same text task", abs(est_v0 - est_text) < 1e-9)
_IMG_TOK["n"] = 50000
est_v = bakeoff.bakeoff(INTENT, candidates=CANDS, prompts=[VISION_TASK], run=False)["estimate_usd"]
ck("with image tokens > 0, the vision estimate is STRICTLY higher (image tokens are in the estimate)",
   est_v > est_text + 1e-9)

# ── (b)+(c) capture the images kwarg on every adapters.call, by sig ──
SEEN = []


def _fake_call(model, prompt, **kw):
    sig = str(kw.get("sig") or "")
    SEEN.append((sig, kw.get("images")))
    if sig.endswith("requirement-extract"):
        return {"parsed": {"requirements": ["faithful to the image; no invented details"]}, "text": "{}",
                "cost": 0.0001, "in_tok": 5, "out_tok": 2}
    if sig.endswith("requirement-screen"):                      # not confident → forces escalation to the adjudicator
        return {"parsed": {"good": True, "usable": True, "score": 1, "confident": False, "requirements_met": []},
                "text": "{}", "cost": 0.0001, "in_tok": 5, "out_tok": 2}
    if sig.endswith("requirement-adjudicate"):
        return {"parsed": {"good": True, "usable": True, "score": 1, "confident": True, "requirements_met": []},
                "text": "{}", "cost": 0.0002, "in_tok": 5, "out_tok": 2}
    if sig.endswith("bakeoff-judge"):
        return {"parsed": {"good": True}, "text": "{}", "cost": 0.0001, "in_tok": 5, "out_tok": 2}
    return {"text": "a caption of the image", "cost": 0.001, "in_tok": 12, "out_tok": 6, "error": None}   # the candidate run


adapters.call = _fake_call


def _imgs_for(suffix):
    return [imgs for sig, imgs in SEEN if sig.endswith(suffix)]


print("\n-- (b) requirement-aware: candidate + SCREEN + ADJUDICATOR see the image; EXTRACTION does not --")
SEEN.clear()
bakeoff.bakeoff(INTENT, candidates=CANDS, prompts=[VISION_TASK], run=True, budget_usd=999, requirement_aware=True)
ck("candidate run RECEIVED images=[PNG]", _imgs_for(INTENT) and _imgs_for(INTENT)[0] == [PNG])
ck("requirement SCREEN judge RECEIVED images=[PNG]", _imgs_for("requirement-screen") and _imgs_for("requirement-screen")[0] == [PNG])
ck("requirement ADJUDICATOR RECEIVED images=[PNG] (screen not confident → escalated)",
   _imgs_for("requirement-adjudicate") and _imgs_for("requirement-adjudicate")[0] == [PNG])
ck("requirement EXTRACTION did NOT receive images (criteria come from the prompt text)",
   _imgs_for("requirement-extract") and _imgs_for("requirement-extract")[0] is None)

print("\n-- (b) generic judge also sees the image --")
SEEN.clear()
bakeoff.bakeoff(INTENT, candidates=CANDS, prompts=[VISION_TASK], run=True, budget_usd=999)
ck("generic bakeoff-judge RECEIVED images=[PNG]", _imgs_for("bakeoff-judge") and _imgs_for("bakeoff-judge")[0] == [PNG])

print("\n-- (c) a plain-STRING task passes NO images kwarg (byte-for-byte) --")
SEEN.clear()
bakeoff.bakeoff(INTENT, candidates=CANDS, prompts=[TASK_TXT], run=True, budget_usd=999)
cand = _imgs_for(INTENT)                                        # the candidate call's sig == the intent
ck("a text task's candidate call has NO images (None captured), never images=[…]", cand and all(i is None for i in cand))
ck("and no call in a text bakeoff carried an image", all(imgs is None for _s, imgs in SEEN))

print(f"\n{'[FAIL]' if fails else 'OK'} test_bakeoff_vision: {len(fails)} failure(s)")
sys.exit(1 if fails else 0)

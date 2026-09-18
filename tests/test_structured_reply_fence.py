"""REGRESSION — a $0 subscription lane wraps its JSON verdict in a ```json fence, so every schema-reply consumer
that read the NONEXISTENT r['json'] key (the adapter surfaces its decode as r['parsed']) fell through to a
fence-BLIND json.loads and decoded NOTHING. The requirement + generic judges then SILENTLY LABELED 0/N (bakeoff
good_rate=null; recommend / effort-titrate / best-value / infer-intent all blind on any lane-served reply) — the
absence-as-success failure this repo exists to prevent.

adapters.structured_reply(r) reads the adapter's OWN fence-tolerant decode. This pins: (1) it reads r['parsed'],
tolerates a bare fenced reply, and does NOT resurrect the dead 'json' key; (2) judge_requirements LABELS a fenced
screen verdict (good is not None), on the screen tier, instead of returning a silent good=None.

Hermetic: adapters.call stubbed to return the exact fenced-reply shape the lane produces; no network, zero spend."""
import os
import sys
import json
import tempfile

os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="spendguard-fence-")
os.environ.setdefault("SPENDGUARD_TEST_ISOLATED", "1")
os.environ.setdefault("SPENDGUARD_NO_AUTOINSTALL", "1")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from spendguard import adapters, requirement_judge   # noqa: E402

fails = []


def ck(name, cond):
    print(("  [OK] " if cond else "  [FAIL] ") + name)
    if not cond:
        fails.append(name)


FENCED = '```json\n{"good": true, "usable": true, "score": 9, "confident": true, "requirements_met": []}\n```'

print("-- structured_reply reads the adapter's fence-tolerant decode, never the dead 'json' key --")
# 1. the adapter populated r['parsed'] (the real path): returned as-is
ck("reads r['parsed'] when present (a dict)",
   adapters.structured_reply({"parsed": {"good": True}, "text": FENCED}) == {"good": True})
# 2. 'parsed' absent, text is a ```json fence: decoded fence-tolerantly (NOT a bare json.loads, which choked here)
_only_text = adapters.structured_reply({"text": FENCED})
ck("decodes a ```json fenced reply when 'parsed' is absent (fence-tolerant fallback)",
   isinstance(_only_text, dict) and _only_text.get("good") is True)
# 3. the OLD field 'json' is DEAD — a result carrying only 'json' (a key the adapter never sets) is not resurrected
ck("does NOT read a legacy 'json' key (the field the adapter never sets → the original bug)",
   adapters.structured_reply({"json": {"good": True}}) is None)
# 4. genuinely undecodable text → None (honest, never a fabricated dict)
ck("undecodable text → None (never a guessed dict)",
   adapters.structured_reply({"text": "sorry, I cannot answer that"}) is None)

print("\n-- judge_requirements LABELS a fenced screen verdict (the bug: it used to return good=None) --")


def _fake_call(model, prompt, **kw):
    sig = str(kw.get("sig") or "")
    if "requirement-extract" in sig:
        body = '{"requirements": ["Exactly two sentences.", "State the cause.", "Faithful to the transcript."]}'
    elif "requirement-screen" in sig:
        # the EXACT bug shape: a CORRECT verdict wrapped in a ```json fence, surfaced as r['parsed'] (fence-stripped)
        body = ('{"good": true, "usable": true, "score": 9, "confident": true, '
                '"requirements_met": [{"requirement": "Exactly two sentences.", "met": true}]}')
    else:
        return {"text": "", "parsed": None, "cost": 0.0, "error": "unexpected sig"}
    return {"text": "```json\n%s\n```" % body, "parsed": json.loads(body), "cost": 0.0, "error": None}


_orig = adapters.call
adapters.call = _fake_call
try:
    requirement_judge._req_cache.clear()
    v = requirement_judge.judge_requirements("Summarize the transcript in exactly two sentences.",
                                             "A faithful two-sentence digest.")
finally:
    adapters.call = _orig

ck("a fenced screen verdict is LABELED (good is not None) — never silently unlabeled", v.get("good") is not None)
ck("the label is the verdict the screen returned (good=True)", v.get("good") is True)
ck("it resolved on the SCREEN tier (confident screen → no needless opus escalation)", v.get("tier") == "screen")

print(f"\n{'[FAIL]' if fails else 'OK'} test_structured_reply_fence: {len(fails)} failure(s)")
sys.exit(1 if fails else 0)

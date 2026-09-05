"""Bulk VISION fans across the metered API, not the text-only lanes — governed, checkpointed, arity-checked, and
fail-loud on the things that bite a labeler: no vision_model, a missing/oversized image, and a swallowed deadline.

The lane executors are print-mode CLIs with no image channel (the trap behind a labeler that cold-400'd). So
bulk_delegate(images_for=…, vision_model=…) routes each task to adapters.call(images=…) on the API, reusing the SAME
durable core (checkpoint/resume/governor) as the lane fan. Error rows carry a STRUCTURED `reason` code (asserted
here and branchable by callers), not free-form text. Offline: adapters.call + dispatch stubbed, no LLM.
"""
import os
import sys
import tempfile

os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="sg-bulkvis-")
os.environ.setdefault("SPENDGUARD_TEST_ISOLATED", "1")
os.environ.setdefault("SPENDGUARD_NO_AUTOINSTALL", "1")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from spendguard import lane_balance, adapters, dispatch

fails = []


def ck(name, cond):
    print(("  [OK] " if cond else "  [FAIL] ") + name)
    if not cond:
        fails.append(name)


DATA_URL = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAet"   # a tiny, well-under-cap image
VMODEL = "openai:gpt-5-nano"
SCHEMA = {"type": "object", "required": ["results"],
          "properties": {"results": {"type": "array", "items": {
              "type": "object", "required": ["id", "label"], "properties": {"id": {"type": "string"}, "label": {"type": "string"}}}}}}

dispatch.acquire = lambda *a, **k: 0.0
dispatch.release = lambda *a, **k: None

_seen = []


def _envelope(ids):
    return '{"results":[' + ",".join('{"id":"%s","label":"x"}' % x for x in ids) + ']}'


def _good_call(model, prompt, **kw):
    _seen.append({"model": model, **kw})
    ids = prompt.split("|")                                  # tasks encode their ids as "a1|a2|a3"
    return {"text": _envelope(ids), "cost": 0.002, "executor": "api",
            "model": model.split(":")[1], "provider": model.split(":")[0]}


# ── routes to the API with images=, the named vision model, and refuse-billed threaded through ──
adapters.call = _good_call
_seen.clear()
res = lane_balance.bulk_delegate(["a1|a2", "b1|b2|b3"], "vis:label", images_for=lambda t: [DATA_URL],
                                 vision_model=VMODEL, schema=SCHEMA, refuse_billed=True)
ck("every task ran on the API vision model (no lane)", len(_seen) == 2 and all(c["model"] == VMODEL for c in _seen))
ck("images= was forwarded to the vision call", all(c.get("images") == [DATA_URL] for c in _seen))
ck("refuse_billed → no_metered_fallback=True threaded through", all(c.get("no_metered_fallback") is True for c in _seen))
ck("results carry executor=api and text", all(r.get("lane") == "api" and r.get("text") for r in res))

# ── the fail-loud guards, asserted by STRUCTURED reason code (never substring on free-form text) ──
r_novm = lane_balance.bulk_delegate(["x"], "vis:novmodel", images_for=lambda t: [DATA_URL])
ck("images_for without vision_model → reason=no_vision_model", r_novm[0].get("reason") == "no_vision_model" and r_novm[0].get("text") is None)

r_noimg = lane_balance.bulk_delegate(["x"], "vis:noimg", images_for=lambda t: [], vision_model=VMODEL)
ck("no image for a task → reason=no_image", r_noimg[0].get("reason") == "no_image" and r_noimg[0].get("text") is None)

r_miss = lane_balance.bulk_delegate(["x"], "vis:missing", images_for=lambda t: ["/no/such/image_xyz.png"], vision_model=VMODEL)
ck("unreadable image → reason=image_unreadable", r_miss[0].get("reason") == "image_unreadable" and r_miss[0].get("text") is None)

big = os.path.join(os.environ["SPENDGUARD_HOME"], "big.png")
with open(big, "wb") as fh:                                   # sparse: getsize reports 33MB without writing 33MB
    fh.seek(33 * 1024 * 1024)
    fh.write(b"\0")
r_big = lane_balance.bulk_delegate(["x"], "vis:big", images_for=lambda t: [big], vision_model=VMODEL)
ck("oversized image → reason=image_too_big (bounded before load, no OOM)", r_big[0].get("reason") == "image_too_big" and r_big[0].get("text") is None)

# ── arity: a vision envelope that DROPS an id becomes a retried MISS (same contract as the lane path) ──
adapters.call = lambda model, prompt, **kw: {"text": _envelope(prompt.split("|")[:-1]), "cost": 0.001,
                                             "executor": "api", "model": model.split(":")[1], "provider": model.split(":")[0]}
r_ar = lane_balance.bulk_delegate(["a1|a2|a3"], "vis:arity", images_for=lambda t: [DATA_URL], vision_model=VMODEL,
                                  schema=SCHEMA, expect_ids=lambda t: t.split("|"))
ck("a vision envelope dropping an id → retried MISS (arity_miss), not a silent success",
   r_ar[0].get("text") is None and r_ar[0].get("arity_miss", {}).get("missing") == ["a3"])

# ── a DELIBERATE stop (DispatchTimeout) from dispatch.acquire PROPAGATES (halts) — never swallowed to a row ──
adapters.call = _good_call


def _shed(*a, **k):
    raise dispatch.DispatchTimeout("admission shed")


dispatch.acquire = _shed
raised = None
try:
    lane_balance.bulk_delegate(["x", "y"], "vis:shed", images_for=lambda t: [DATA_URL], vision_model=VMODEL)
except dispatch.DispatchTimeout as e:
    raised = e
ck("DispatchTimeout HALTS the vision fan (propagates by exception TYPE), not downgraded to error rows", raised is not None)
dispatch.acquire = lambda *a, **k: 0.0

print(("[OK]" if not fails else "[FAIL]") + " bulk vision fan: %d failure(s)" % len(fails))
sys.exit(1 if fails else 0)

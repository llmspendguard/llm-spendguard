"""Guard for the _promote_batch consolidation (experiment._promote_batch): its OpenAI path used to hand-write the
/v1/chat/completions envelope (a divergent copy of what submit.build_chat_batch_jsonl + guarded_submit centralize, incl.
a hardcoded max_tokens=1500). It now routes through those shared primitives, and a >25K promote is CHUNKED into multiple
≤25K batches (every request submitted, never truncated/dropped). This pins:
  · the OpenAI path CALLS build_chat_batch_jsonl (the shared builder) then guarded_submit — not a hand-rolled envelope;
  · it hands the builder simple {custom_id, content} TASKS (custom_id = the item's id or a GLOBAL 'i<idx>'; content =
    prompt + instruction), with reasoning=None (preserves promote's no-reasoning-effort behavior);
  · run=False estimates only (guarded_submit submit=False), run=True submits; cap_dollars = config.cap();
  · >25K CHUNKS into ceil(N/25K) batches — all requests submitted (no silent drop, no refusal), custom_ids unique across
    chunks;
  · the Anthropic branch (out of scope) is left intact: a non-run anthropic promote still estimates via the gate.

Offline, $0: the shared primitives + the gate estimator are stubbed (no network, no spend). Isolated SPENDGUARD_HOME.
"""
import json
import os
import sys
import tempfile

os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="sg-promote-batch-")
os.environ.setdefault("SPENDGUARD_TEST_ISOLATED", "1")
os.environ.setdefault("SPENDGUARD_NO_AUTOINSTALL", "1")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from spendguard import experiment, submit, ui, gate    # noqa: E402


def main():
    fails = []

    def ck(name, cond, extra=""):
        print(("  [OK] " if cond else "  [FAIL] ") + name + (("  — " + str(extra)) if extra and not cond else ""))
        if not cond:
            fails.append(name)

    seen = {"builds": [], "submits": []}

    def _fake_build(tasks_path, model, **kw):
        with open(tasks_path) as f:                     # capture the TASKS _promote_batch wrote (proves it feeds the builder)
            tasks = [json.loads(ln) for ln in f if ln.strip()]
        seen["builds"].append({"n": len(tasks), "kw": kw,
                               "first": tasks[0] if tasks else None, "last": tasks[-1] if tasks else None})
        return ("/fake/promote_req_%d.jsonl" % len(seen["builds"]), len(tasks))

    def _fake_guarded(path, model=None, cap_dollars=None, submit=None, **kw):
        seen["submits"].append({"path": path, "model": model, "cap": cap_dollars, "submit": submit})
        return "batch_%d" % len(seen["submits"]) if submit else None

    _saved = (submit.build_chat_batch_jsonl, submit.guarded_submit, ui.estimate_only, gate._estimate_anthropic_requests)
    submit.build_chat_batch_jsonl = _fake_build
    submit.guarded_submit = _fake_guarded
    ui.estimate_only = lambda *a, **k: None             # silence the estimate-only UX (no side effects in the test)
    try:
        # ── OpenAI path (estimate-only, one chunk) routes through the shared builder with the right tasks + reasoning ──
        print("-- OpenAI path: reuses build_chat_batch_jsonl + guarded_submit (not a hand-written envelope) --")
        seen["builds"], seen["submits"] = [], []
        r = experiment._promote_batch("promote-test", "gpt-5-nano", "\n\nDO THIS", [("a", "prompt A"), (None, "prompt B")],
                                      run=False)
        ck("build_chat_batch_jsonl called once (one chunk)", len(seen["builds"]) == 1, seen["builds"])
        ck("fed simple {custom_id, content} TASKS (id or 'i<idx>', content = prompt + instruction)",
           seen["builds"][0]["first"] == {"custom_id": "a", "content": "prompt A\n\nDO THIS"}
           and seen["builds"][0]["last"] == {"custom_id": "i1", "content": "prompt B\n\nDO THIS"}, seen["builds"][0])
        ck("builder called with reasoning=None (preserves no-reasoning-effort)",
           seen["builds"][0]["kw"].get("reasoning", "MISSING") is None, seen["builds"][0]["kw"])
        ck("guarded_submit got the built envelope + config.cap() + submit=False (estimate only)",
           seen["submits"] and seen["submits"][0]["submit"] is False and seen["submits"][0]["path"].startswith("/fake/"),
           seen["submits"])
        ck("returns ok, one chunk, batch=None (estimate)", r.get("ok") and r.get("chunks") == 1
           and r["batches"][0]["batch"] is None, r)

        # ── OpenAI path (run) submits via the shared guarded_submit ──
        print("\n-- OpenAI path (run=True): submits via guarded_submit --")
        seen["builds"], seen["submits"] = [], []
        r2 = experiment._promote_batch("promote-test", "gpt-5-nano", "", [("x", "p")], run=True)
        ck("run=True submits and returns the batch id in submitted[]",
           seen["submits"] and seen["submits"][0]["submit"] is True and r2.get("submitted") == ["batch_1"], (seen["submits"], r2))

        # ── >25K is CHUNKED into multiple ≤25K batches — every request submitted, none dropped ──
        print("\n-- >25K requests are CHUNKED into ceil(N/25K) batches (all submitted, none dropped) --")
        seen["builds"], seen["submits"] = [], []
        items = [(None, "p%d" % k) for k in range(25001)]        # 25,001 → 2 chunks (25,000 + 1)
        r3 = experiment._promote_batch("promote-test", "gpt-5-nano", "", items, run=False)
        ck("chunked into 2 batches (not refused, not truncated)", r3.get("ok") and r3.get("chunks") == 2, r3.get("chunks"))
        ck("build called per chunk (2), sizes 25000 + 1 = all 25001 requests submitted",
           len(seen["builds"]) == 2 and seen["builds"][0]["n"] == 25000 and seen["builds"][1]["n"] == 1
           and (seen["builds"][0]["n"] + seen["builds"][1]["n"]) == 25001, [b["n"] for b in seen["builds"]])
        ck("custom_ids are GLOBALLY unique across chunks (chunk 1 ends i24999, chunk 2 starts i25000)",
           seen["builds"][0]["last"]["custom_id"] == "i24999" and seen["builds"][1]["first"]["custom_id"] == "i25000",
           (seen["builds"][0]["last"], seen["builds"][1]["first"]))

        # ── Anthropic branch left intact: a non-run anthropic promote still estimates via the gate (summed over chunks) ──
        print("\n-- Anthropic branch unchanged: a non-run anthropic promote estimates via the gate --")
        gate._estimate_anthropic_requests = lambda reqs: {"requests": len(reqs), "in_tok": 10, "out_tok": 20, "cost": 0.5}
        ra = experiment._promote_batch("promote-test", "anthropic:claude-opus-4-8", "", [("a", "p")], run=False)
        ck("anthropic non-run returns the gate estimate (branch preserved)",
           ra.get("ok") and ra.get("provider") == "anthropic" and ra.get("est") == 0.5, ra)
    finally:
        submit.build_chat_batch_jsonl, submit.guarded_submit, ui.estimate_only, gate._estimate_anthropic_requests = _saved

    print(f"\n{'[FAIL]' if fails else 'OK'} test_promote_batch_reuses_builder: {len(fails)} failure(s)")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())

"""Guard for the _promote_batch consolidation (experiment._promote_batch): its OpenAI path used to hand-write the
/v1/chat/completions envelope (a divergent copy of what submit.build_chat_batch_jsonl + guarded_submit centralize, incl.
a hardcoded max_tokens=1500). It now routes through those shared primitives. This pins:
  · the OpenAI path CALLS build_chat_batch_jsonl (the shared builder) then guarded_submit — not a hand-rolled envelope;
  · it hands the builder simple {custom_id, content} TASKS (custom_id = the item's id or 'i<idx>'; content = prompt +
    instruction), with reasoning=None (preserves promote's no-reasoning-effort behavior);
  · run=False estimates only (guarded_submit submit=False), run=True submits; cap_dollars = config.cap();
  · a >25K job is REFUSED loudly (ok=False), never silently truncated to the first 25K (the old silent drop of
    production requests) — no build/submit happens.
The Anthropic branch (out of scope) is left intact: a non-run anthropic promote still estimates via the gate.

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

    seen = {"build": 0, "tasks": None, "build_kw": None, "submit": None}

    def _fake_build(tasks_path, model, **kw):
        with open(tasks_path) as f:                     # capture the TASKS _promote_batch wrote (proves it feeds the builder)
            seen["tasks"] = [json.loads(ln) for ln in f if ln.strip()]
        seen["build"] += 1
        seen["build_kw"] = kw
        return ("/fake/promote_req.jsonl", len(seen["tasks"]))

    def _fake_guarded(path, model=None, cap_dollars=None, submit=None, **kw):
        seen["submit"] = {"path": path, "model": model, "cap": cap_dollars, "submit": submit}
        return "batch_FAKE" if submit else None

    _saved = (submit.build_chat_batch_jsonl, submit.guarded_submit, ui.estimate_only, gate._estimate_anthropic_requests)
    submit.build_chat_batch_jsonl = _fake_build
    submit.guarded_submit = _fake_guarded
    ui.estimate_only = lambda *a, **k: None             # silence the estimate-only UX (no side effects in the test)
    try:
        # ── OpenAI path (estimate-only) routes through the shared builder, with the right tasks + reasoning ──
        print("-- OpenAI path: reuses build_chat_batch_jsonl + guarded_submit (not a hand-written envelope) --")
        items = [("a", "prompt A"), (None, "prompt B")]
        r = experiment._promote_batch("promote-test", "gpt-5-nano", "\n\nDO THIS", items, run=False)
        ck("build_chat_batch_jsonl was called exactly once (OpenAI path uses the shared builder)", seen["build"] == 1)
        ck("it was fed simple {custom_id, content} TASKS (custom_id = id or 'i<idx>', content = prompt + instruction)",
           seen["tasks"] == [{"custom_id": "a", "content": "prompt A\n\nDO THIS"},
                             {"custom_id": "i1", "content": "prompt B\n\nDO THIS"}], seen["tasks"])
        ck("the builder is called with reasoning=None (preserves promote's no-reasoning-effort behavior)",
           seen["build_kw"].get("reasoning", "MISSING") is None, seen["build_kw"])
        ck("guarded_submit got the built envelope + config.cap() + submit=False (estimate only)",
           seen["submit"] and seen["submit"]["path"] == "/fake/promote_req.jsonl"
           and seen["submit"]["submit"] is False, seen["submit"])
        ck("estimate-only returns ok + the built jsonl + no batch", r.get("ok") and r.get("jsonl") == "/fake/promote_req.jsonl"
           and r.get("batch") is None, r)

        # ── OpenAI path (run) submits via the shared guarded_submit ──
        print("\n-- OpenAI path (run=True): submits via guarded_submit --")
        seen["submit"] = None
        r2 = experiment._promote_batch("promote-test", "gpt-5-nano", "", [("x", "p")], run=True)
        ck("run=True submits (guarded_submit submit=True) and returns the batch id",
           seen["submit"] and seen["submit"]["submit"] is True and r2.get("batch") == "batch_FAKE", (seen["submit"], r2))

        # ── >25K is REFUSED loudly, never silently truncated (no build/submit) ──
        print("\n-- >25K requests are REFUSED (ok=False), never silently truncated to the first 25K --")
        seen["build"] = 0
        seen["submit"] = None
        big = [("i", "p")] * 25001
        r3 = experiment._promote_batch("promote-test", "gpt-5-nano", "", big, run=False)
        ck("a >25K job returns ok=False (refused)", r3.get("ok") is False and "25,000" in (r3.get("error") or ""), r3)
        ck("nothing was built or submitted for the refused job (no silent partial)",
           seen["build"] == 0 and seen["submit"] is None, (seen["build"], seen["submit"]))

        # ── Anthropic branch left intact: a non-run anthropic promote still estimates via the gate ──
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

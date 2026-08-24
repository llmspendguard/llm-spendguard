"""bulk_delegate — fan a batch of similar tasks across ALL good lanes concurrently, governed by dispatch, CHUNKED +
CHECKPOINTED so a crash resumes. Guards: _bulk_arms keeps each lane's BEST use-name and drops a proven-loser lane
(tried, won nothing) while keeping an untried one; the run spreads across the good lanes (round-robin); one erroring
task never wedges the batch; and a checkpoint makes a re-run SKIP finished tasks (resume, no re-pay). Offline:
adapters.call + dispatch stubbed, no LLM, no subprocess.
"""
import os
import sys
import tempfile

os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="sg-bulk-")
os.environ.setdefault("SPENDGUARD_TEST_ISOLATED", "1")
os.environ.setdefault("SPENDGUARD_NO_AUTOINSTALL", "1")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from spendguard import lane_balance, lane_catalog, lane_bandit, adapters, dispatch                     # noqa: E402


def ck(name, cond):
    ok = bool(cond)
    print(("  [OK] " if ok else "  [FAIL] ") + name)
    return [] if ok else [name]


fails = []
lane_catalog.arms = lambda flt=None: [("gemini", "g-low"), ("gemini", "g-high"), ("codex", "gpt-5.5"), ("zai-coding", "glm")]
lane_catalog.lane_provider = lambda l: {"gemini": "gemini", "codex": "openai", "zai-coding": "zai"}.get(l)
lane_bandit._arm_cooling = lambda l, u: False
lane_bandit.arm_stats = lambda intent: {
    ("gemini", "g-low"): {"winrate": 0.0, "trials": 2},     # gemini's WEAK variant (won nothing) — not the lane's best
    ("gemini", "g-high"): {"winrate": 1.0, "trials": 2},    # gemini's best variant
    ("codex", "gpt-5.5"): {"winrate": 1.0, "trials": 2},    # codex good
    ("zai-coding", "glm"): {"winrate": 0.0, "trials": 3},   # zai PROVEN loser for this intent → whole lane dropped
}

print("-- _bulk_arms: one BEST use-name per lane; a proven-loser lane dropped; untried kept --")
arms = lane_balance._bulk_arms("myintent")
fails += ck("gemini→g-high (its best) + codex; zai dropped (won nothing); gemini-low not chosen",
            set(arms) == {("gemini", "g-high"), ("codex", "gpt-5.5")})
lane_bandit.arm_stats = lambda intent: {}     # a cold intent (no trials) → every lane kept (explore)
fails += ck("a COLD intent keeps every lane (untried = optimistic explore)",
            {a[0] for a in lane_balance._bulk_arms("cold")} == {"gemini", "codex", "zai-coding"})
# the rest of the run uses a clean TWO-good-lane world so the round-robin spread is unambiguous
lane_catalog.arms = lambda flt=None: [("gemini", "g-high"), ("codex", "gpt-5.5")]
lane_bandit.arm_stats = lambda intent: {("gemini", "g-high"): {"winrate": 1.0, "trials": 2},
                                        ("codex", "gpt-5.5"): {"winrate": 1.0, "trials": 2}}

class _CallRecorder:
    """A stand-in for adapters.call that records calls in INSTANCE state (self.calls, mutated via the self param —
    no free module list to orphan). It mirrors adapters.call's REAL signature and CONTRACT so bugs the earlier **kw
    stub hid — an unknown kwarg (`_no_sub`), no output budget — fail this test offline. It also HONORS
    no_metered_fallback: for the 'WOULDBILL' sentinel it returns a $cost answer normally, or a refusal error row when
    no_metered_fallback is set — so --refuse-billed is provable without a real lane."""

    def __init__(self):
        self.calls = []

    def __call__(self, model, prompt, max_tokens=None, system=None, reasoning=None, schema=None,
                 timeout_s=None, sig=None, retries=2, files=None, _no_guard=False, no_metered_fallback=False):
        assert max_tokens is not None or sig, "adapters.call needs max_tokens or sig (an output budget)"
        self.calls.append({"model": model, "prompt": prompt, "system": system, "no_metered_fallback": no_metered_fallback})
        if prompt == "BOOM":
            raise RuntimeError("boom")
        if prompt == "WOULDBILL":                        # a task the lane misses → would fall back to metered
            if no_metered_fallback:
                return {"text": None, "error": "refused: would bill metered API", "cost": None}
            return {"text": "billed-answer", "cost": 0.01}   # fallback allowed → a metered ($cost) answer
        return {"text": f"ans::{model}::{prompt}", "cost": 0}


_rec = _CallRecorder()
adapters.call = _rec
dispatch.acquire = lambda *a, **k: 0.0
dispatch.release = lambda *a, **k: None

print("\n-- fans across the good lanes (round-robin), results in task order --")
res = lane_balance.bulk_delegate(["t0", "t1", "t2", "t3"], "myintent")
fails += ck("all 4 tasks answered, in order", len(res) == 4 and all(res[i]["text"] and f"t{i}" in res[i]["text"] for i in range(4)))
fails += ck("spread across BOTH good lanes (both use-names appear)",
            {res[i]["use_name"] for i in range(4)} == {"g-high", "gpt-5.5"})

print("\n-- one erroring task does NOT wedge the batch --")
res2 = lane_balance.bulk_delegate(["a", "BOOM", "c"], "myintent")
fails += ck("the bad task carries an error; the others still succeed",
            res2[0]["text"] and res2[1].get("error") and res2[2]["text"])

print("\n-- checkpoint: a re-run RESUMES (skips finished tasks, no re-pay) --")
ckpath = os.path.join(os.environ["SPENDGUARD_HOME"], "ck.jsonl")
lane_balance.bulk_delegate(["x", "y", "z"], "myintent", checkpoint=ckpath)
n_before = len(_rec.calls)
res3 = lane_balance.bulk_delegate(["x", "y", "z"], "myintent", checkpoint=ckpath)   # same tasks + checkpoint → resume
fails += ck("re-run made ZERO new calls (fully resumed from checkpoint)", len(_rec.calls) == n_before)
fails += ck("resumed results are intact", all(r.get("text") for r in res3))

print("\n-- rows carry the MODEL that answered (provenance for trust-labeled artifacts) --")
resm = lane_balance.bulk_delegate(["m0", "m1"], "myintent")
fails += ck("every row has a 'model' field", all(r.get("model") for r in resm))

print("\n-- --system reaches adapters.call on every task (sent once, not duplicated into each) --")
_rec.calls.clear()
lane_balance.bulk_delegate(["s0", "s1"], "myintent", system="SHARED-INSTRUCTION")
fails += ck("system passed through to every underlying call", _rec.calls and all(c["system"] == "SHARED-INSTRUCTION" for c in _rec.calls))

print("\n-- refuse_billed: no_metered_fallback reaches the call; a would-bill task ERRORS, never bills --")
_rec.calls.clear()
resr = lane_balance.bulk_delegate(["WOULDBILL"], "myintent", refuse_billed=True)
fails += ck("refuse_billed → no_metered_fallback=True on the call", all(c["no_metered_fallback"] for c in _rec.calls))
fails += ck("would-bill task returns an error row, billed=False (no spend)", resr[0].get("error") and not resr[0].get("billed"))
resb = lane_balance.bulk_delegate(["WOULDBILL"], "myintent")     # default: fallback allowed
fails += ck("default (no refuse) lets it fall back to metered (billed=True)", resb[0].get("billed") is True)

print("\n-- checkpoint keyed by CONTENT: a REORDERED list maps by meaning; a stale POSITIONAL checkpoint is ignored --")
ckc = os.path.join(os.environ["SPENDGUARD_HOME"], "ck_content.jsonl")
lane_balance.bulk_delegate(["A", "B", "C"], "myintent", checkpoint=ckc)
_rec.calls.clear()
res_re = lane_balance.bulk_delegate(["C", "A", "B"], "myintent", checkpoint=ckc)   # SAME tasks, DIFFERENT order
fails += ck("reordered re-run made ZERO new calls (content-keyed, not positional)", len(_rec.calls) == 0)
fails += ck("each reordered task got ITS OWN content's result (C→C, A→A, B→B)",
            "::C" in (res_re[0].get("text") or "") and "::A" in (res_re[1].get("text") or "")
            and "::B" in (res_re[2].get("text") or ""))
import json as _json                                                              # noqa: E402
stale = os.path.join(os.environ["SPENDGUARD_HOME"], "ck_positional.jsonl")
with open(stale, "w") as f:                                                       # an OLD positional-format checkpoint
    f.write(_json.dumps({"i": 0, "r": {"text": "WRONG-STALE", "lane": "x"}}) + "\n")
_rec.calls.clear()
res_st = lane_balance.bulk_delegate(["fresh0", "fresh1"], "myintent", checkpoint=stale)
fails += ck("stale POSITIONAL checkpoint IGNORED — task 0 re-run, never given WRONG-STALE",
            "WRONG-STALE" not in (res_st[0].get("text") or "") and len(_rec.calls) >= 2)

print(f"\n{'[FAIL]' if fails else 'OK'} test_bulk_delegate: {len(fails)} failure(s)")
sys.exit(1 if fails else 0)

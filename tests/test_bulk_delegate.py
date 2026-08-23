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
    no free module list to orphan). It mirrors adapters.call's REAL signature and CONTRACT so two bugs the earlier
    **kw stub hid — passing an unknown kwarg (`_no_sub`), or naming NO output budget — fail this test offline
    instead of only on the real lane path."""

    def __init__(self):
        self.calls = []

    def __call__(self, model, prompt, max_tokens=None, system=None, reasoning=None, schema=None,
                 timeout_s=None, sig=None, retries=2, files=None, _no_guard=False):
        assert max_tokens is not None or sig, "adapters.call needs max_tokens or sig (an output budget)"
        self.calls.append((model, prompt))
        if prompt == "BOOM":
            raise RuntimeError("boom")
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

print(f"\n{'[FAIL]' if fails else 'OK'} test_bulk_delegate: {len(fails)} failure(s)")
sys.exit(1 if fails else 0)

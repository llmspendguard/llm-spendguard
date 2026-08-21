"""Decaying contextual bandit — the learned cross-lane router's PURE logic (no LLM). Guards the behaviours Ash asked
for: EQUAL-START (every untried arm explored before any is exploited), EXPLOIT-the-winner, the ε-exploration floor,
cooling arms skipped, exponential-forgetting DECAY (relearn as models drift), and bake-off PACING (cold→always,
<2 live arms→never). Offline: isolated db, seeded RNG, idle/cost tilts stubbed so the exploit ranking is pure
win-rate.
"""
import os
import sys
import tempfile

os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="sg-bandit-")
os.environ.setdefault("SPENDGUARD_TEST_ISOLATED", "1")
os.environ.setdefault("SPENDGUARD_NO_AUTOINSTALL", "1")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from spendguard import lane_bandit as lb                                               # noqa: E402


def ck(name, cond):
    ok = bool(cond)
    print(("  [OK] " if ok else "  [FAIL] ") + name)
    return [] if ok else [name]


fails = []
ARMS = [("gemini", "g-low"), ("codex", "gpt-5.5"), ("zai-coding", "glm-5.3")]
# neutralize idle/cost tilts + cooling so the exploit ranking is PURE decayed win-rate and choices are deterministic
lb._idle_bonus = lambda lane: 1.0
lb._cost_bonus = lambda lane, un: 1.0
lb._arm_cooling = lambda lane, un: False

print("-- EQUAL-START: every untried arm is explored before any repeats --")
seen = []
for _ in range(3):
    a = lb.choose_arm("intentX", ARMS)
    seen.append(a)
    lb.record_trial("intentX", a[0], a[1], won=0.5)     # neutral outcome, just to mark it tried
fails += ck("all 3 arms tried before any repeat (equal-start)", set(seen) == set(ARMS))

print("\n-- EXPLOIT: after learning a clear winner, it is chosen the majority of the time --")
for _ in range(6):
    lb.record_trial("intentY", "gemini", "g-low", won=1.0)      # gemini wins
    lb.record_trial("intentY", "codex", "gpt-5.5", won=0.0)     # codex loses
    lb.record_trial("intentY", "zai-coding", "glm-5.3", won=0.0)
lb._rng.seed(1)
picks = [lb.choose_arm("intentY", ARMS) for _ in range(20)]
fails += ck("the learned winner (gemini) is the plurality pick", picks.count(("gemini", "g-low")) >= 12)
st = lb.arm_stats("intentY")
fails += ck("winner has the highest decayed win-rate",
            st[("gemini", "g-low")]["winrate"] > st[("codex", "gpt-5.5")]["winrate"])
fails += ck("ε-floor still explored a loser at least once (never fully abandoned)",
            any(p != ("gemini", "g-low") for p in picks))

print("\n-- DECAY (relearn): recent losses pull the old winner's win-rate DOWN --")
wr_before = lb.arm_stats("intentY")[("gemini", "g-low")]["winrate"]
for _ in range(6):
    lb.record_trial("intentY", "gemini", "g-low", won=0.0)      # gemini now losing
wr_after = lb.arm_stats("intentY")[("gemini", "g-low")]["winrate"]
fails += ck("decayed win-rate falls after recent losses (non-stationary)", wr_after < wr_before - 0.2)

print("\n-- COOLING arms are skipped; None when all cool --")
lb._arm_cooling = lambda lane, un: (lane == "gemini")
a = lb.choose_arm("intentY", ARMS)
fails += ck("a cooling arm (gemini) is never chosen", a is not None and a[0] != "gemini")
lb._arm_cooling = lambda lane, un: True
fails += ck("all arms cooling → None (upstream falls back to API)", lb.choose_arm("intentY", ARMS) is None)
lb._arm_cooling = lambda lane, un: False

print("\n-- BAKE-OFF pacing --")
fails += ck("a cold intent always bake-offs (learn fast)", lb.should_bakeoff("coldIntent", ARMS) is True)
fails += ck("fewer than 2 live arms → never bake-offs", lb.should_bakeoff("intentY", ARMS[:1]) is False)

print("\n-- JUDGE: an EMPTY side loses WITHOUT spending a judge call (pure) --")
fails += ck("A empty → B (codex) wins, no LLM", lb.bakeoff_judge("t", "", "real", ("gemini", "g"), ("codex", "c"))[0] == ("codex", "c"))
fails += ck("B empty → A (gemini) wins, no LLM", lb.bakeoff_judge("t", "real", "", ("gemini", "g"), ("codex", "c"))[0] == ("gemini", "g"))
fails += ck("both empty → no winner", lb.bakeoff_judge("t", "", "", ("gemini", "g"), ("codex", "c"))[0] is None)

print("\n-- run_bakeoff / bandit_call: records the winner, returns a usable answer (judge + lane run stubbed) --")
from spendguard import lane_catalog                                                    # noqa: E402
_o_arms, _o_judge, _o_runarm = lane_catalog.arms, lb.bakeoff_judge, lb._run_arm
try:
    lane_catalog.arms = lambda flt=None: [("gemini", "g-low"), ("codex", "gpt-5.5")]
    lb.bakeoff_judge = lambda task, oa, ob, aa, ab: (aa, "A wins")     # the FIRST arm always wins
    lb._run_arm = lambda arm, *a, **k: f"out-{arm[0]}"                 # each lane returns a tagged answer
    out = lb.run_bakeoff("intentZ", "do X")
    fails += ck("run_bakeoff returns the WINNER's output + lane", out and out["text"] == "out-gemini" and out["lane"] == "gemini")
    stz = lb.arm_stats("intentZ")
    fails += ck("winner gemini recorded a win (winrate high)", stz[("gemini", "g-low")]["winrate"] > 0.9)
    fails += ck("loser codex recorded a loss (tried, winrate low)",
                stz[("codex", "gpt-5.5")]["trials"] > 0.9 and stz[("codex", "gpt-5.5")]["winrate"] < 0.1)
    out2 = lb.bandit_call("intentW", "do Y")     # cold intent → bake-off path → winner's answer
    fails += ck("bandit_call on a cold intent returns the answer + which lane served it",
                out2 and out2["text"] == "out-gemini" and out2["lane"] == "gemini")
finally:
    lane_catalog.arms, lb.bakeoff_judge, lb._run_arm = _o_arms, _o_judge, _o_runarm

print(f"\n{'[FAIL]' if fails else 'OK'} test_lane_bandit: {len(fails)} failure(s)")
sys.exit(1 if fails else 0)

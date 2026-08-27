"""The unified per-resource STATE store — axis 1 (COOLDOWN {until, reason}). This is the consolidation target for
the scattered cooldown flags; it must carry the REASON the old code threw away, be multi-state-able (a whole lane
and a specific (lane,model) cool INDEPENDENTLY), never let a later cool be shortened by an earlier one, drop
expired cooldowns, and PERSIST across processes.
"""
import os
import sys
import time
import tempfile

os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="sg-rstate-")
os.environ.setdefault("SPENDGUARD_TEST_ISOLATED", "1")
os.environ.setdefault("SPENDGUARD_NO_AUTOINSTALL", "1")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from spendguard import resource_state as rs                                            # noqa: E402

fails = []


def check(name, cond):
    ok = bool(cond)
    print(("  [OK] " if ok else "  [FAIL] ") + name)
    if not ok:
        fails.append(name)


rs._reset()

print("-- COOLDOWN carries a reason, and clears cleanly --")
k = rs.lane_key("gemini")
rs.cool(k, 100, "quota")
check("cooling True + reason preserved", rs.cooling(k) and rs.cool_reason(k) == "quota")
check("cool_until is ~now+100", 95 < rs.cool_until(k) - time.time() <= 100)
rs.clear_cooldown(k)
check("clear_cooldown → not cooling, no reason", not rs.cooling(k) and rs.cool_reason(k) == "")

print("\n-- MULTI-STATE-ABLE: a whole lane and a (lane,model) cool INDEPENDENTLY --")
rs._reset()
rs.cool(rs.lane_model_key("codex", "gpt-5-nano"), 100, "model-miss")
check("the (lane,model) is cooling", rs.cooling(rs.lane_model_key("codex", "gpt-5-nano")))
check("...but the WHOLE codex lane is NOT (distinct key)", not rs.cooling(rs.lane_key("codex")))

print("\n-- a LATER cool extends; an EARLIER one never shortens an active cool --")
rs._reset()
rs.cool(k, 1000, "quota")
rs.cool(k, 10, "rate")                              # a shorter window must NOT undercut the active quota window
check("the shorter cool did not shorten it", rs.cool_until(k) - time.time() > 900)
check("...and the original reason is kept", rs.cool_reason(k) == "quota")
rs.cool(k, 5000, "quota")                           # a longer window DOES extend
check("a longer cool extends", rs.cool_until(k) - time.time() > 4000)

print("\n-- expired cooldowns are pruned (a stale 'until' never reads as cooling) --")
rs._reset()
rs.set_cooldown(k, time.time() - 5, "quota")        # already in the past
check("an expired cool reads as NOT cooling", not rs.cooling(k) and rs.cool_until(k) == 0.0)

print("\n-- PERSISTS across processes (a fresh load honours a still-active cool) --")
rs._reset()
rs.cool(k, 3600, "quota")
rs._save()
rs._reset()                                         # simulate a fresh process: memory empty
check("memory cleared → not cooling", not rs.cooling(k))
rs._load_state()
check("a fresh process reload honours the persisted cool", rs.cooling(k) and rs.cool_reason(k) == "quota")

print(f"\n{'[FAIL]' if fails else 'OK'} test_resource_state: {len(fails)} failure(s)")
sys.exit(1 if fails else 0)

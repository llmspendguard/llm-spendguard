"""The unified per-resource STATE store — axis 1 (COOLDOWN {until, reason}) and axis 2 (SIZE_CEILING {value, until,
proven_good}). This is the consolidation target for the scattered in-memory flags. Cooldown must carry the REASON
the old code threw away, be multi-state-able (a whole lane and a specific (lane,model) cool INDEPENDENTLY), never
let a later cool be shortened by an earlier one, drop expired cooldowns, and PERSIST across processes. Size-ceiling
must min-ratchet, gate on a proven-good watermark that NEVER expires, EXPIRE the ceiling after a re-test window (so
a one-off failure can't bypass a lane forever — the poison that starved gemini), PERSIST within that window, and
be INDEPENDENT of the cooldown axis on the same resource.
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

print("\n-- AXIS 2 size_ceiling: one-way-DOWN ratchet + a proven-good watermark that gates it --")
rs._reset()
os.environ["SPENDGUARD_SIZE_CEILING_RETEST_S"] = "3600"     # keep ceilings live through the ratchet checks
sk = rs.lane_key("codex")
rs.set_size_ceiling(sk, 8000)
check("a learned ceiling reads back", rs.size_ceiling(sk) == 8000)
rs.set_size_ceiling(sk, 5000)
check("a SMALLER failing size LOWERS the ceiling (min-ratchet)", rs.size_ceiling(sk) == 5000)
rs.set_size_ceiling(sk, 9000)
check("a LARGER failing size never RAISES it (the ratchet is one-way down)", rs.size_ceiling(sk) == 5000)
rs.note_proven_good(sk, 4000)
rs.note_proven_good(sk, 1000)                              # a smaller success never lowers the watermark
check("proven-good keeps the LARGEST success", rs.proven_good(sk) == 4000)

print("\n-- MULTI-STATE-ABLE: the same lane is cooling AND size-limited on INDEPENDENT axes --")
rs._reset()
rs.cool(sk, 100, "quota")
rs.set_size_ceiling(sk, 5000)
check("both axes are live at once", rs.cooling(sk) and rs.size_ceiling(sk) == 5000)
rs.clear_cooldown(sk)
check("clearing the cooldown leaves the size ceiling intact", not rs.cooling(sk) and rs.size_ceiling(sk) == 5000)

print("\n-- size ceilings PERSIST within their window (a fresh process honours a still-live ceiling) --")
rs._reset()
rs.set_size_ceiling(sk, 7000)
rs.note_proven_good(sk, 3000)
rs._save()
rs._reset()
check("memory cleared → no ceiling", rs.size_ceiling(sk) is None)
rs._load_state()
check("a fresh process reload honours a still-live ceiling", rs.size_ceiling(sk) == 7000)
check("...and the proven-good watermark survived the round-trip", rs.proven_good(sk) == 3000)

print("\n-- past its RE-TEST window a ceiling EXPIRES (re-try the lane) but proven-good NEVER expires --")
rs._reset()
rs.note_proven_good(sk, 3000)
os.environ["SPENDGUARD_SIZE_CEILING_RETEST_S"] = "-1"      # stamp it already-expired: deterministic, no wall-clock sleep
rs.set_size_ceiling(sk, 6000)
check("an expired ceiling reads as None → the big prompt re-tests the lane", rs.size_ceiling(sk) is None)
check("...but the proven-good watermark is untouched by expiry", rs.proven_good(sk) == 3000)
rs._save(); rs._reset(); rs._load_state()                 # _prune drops the stale ceiling, KEEPS the watermark
check("after prune+reload: stale ceiling gone, watermark kept", rs.size_ceiling(sk) is None and rs.proven_good(sk) == 3000)
os.environ["SPENDGUARD_SIZE_CEILING_RETEST_S"] = "3600"

print("\n-- clear_size_ceiling drops the ACTIVE ceiling but KEEPS the proven-good watermark --")
rs._reset()
rs.set_size_ceiling(sk, 5000)
rs.note_proven_good(sk, 2500)
rs.clear_size_ceiling(sk)
check("clear_size_ceiling removes the ceiling", rs.size_ceiling(sk) is None)
check("...and preserves the proven-good watermark (positive fact)", rs.proven_good(sk) == 2500)

print("\n-- a CORRUPT persisted record degrades to 'no active state', never RAISES on the hot dispatch path --")
rs._reset()
bad = rs.lane_key("badlane")
rs._state[bad] = {"cooldown": {"until": "not-a-number", "reason": "x"},          # a truncated/hand-edited state file
                  "size_ceiling": {"value": "NaN", "until": "soon", "proven_good": "lots"}}
try:
    _cool_ok = (not rs.cooling(bad)) and rs.cool_until(bad) == 0.0
    _ceil_ok = rs.size_ceiling(bad) is None            # ABSENT (None), NOT a 0-char limit that would disable the lane
    _pg_ok = rs.proven_good(bad) == 0
    rs._save(); rs._reset(); rs._load_state()          # _prune + cross-process reload over the corrupt file
    _no_raise = True
except Exception as e:                                 # a ValueError here would crash EVERY call that consults the store
    _cool_ok = _ceil_ok = _pg_ok = _no_raise = False
    print(f"    (raised: {type(e).__name__}: {e})")
check("corrupt cooldown 'until' → NOT cooling (no ValueError crash)", _cool_ok)
check("corrupt ceiling 'value' → ABSENT/None (never a 0-char limit that disables the lane — the poison direction)", _ceil_ok)
check("corrupt proven-good → 0 (conservative)", _pg_ok)
check("prune + cross-process reload over a corrupt file does not raise", _no_raise)

print(f"\n{'[FAIL]' if fails else 'OK'} test_resource_state: {len(fails)} failure(s)")
sys.exit(1 if fails else 0)

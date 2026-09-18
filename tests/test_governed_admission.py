"""dispatch.admit — the ONE owner of the governed shed-to-metered + deadline-split policy, shared by vendor_call.call
AND adapters.call(governed=True) so the two entries can never drift (warden's ask: a governed entry that keeps
adapters.call's contract). Pins the policy directly:
  - a free lane admits (held, not shed);
  - a saturated $0 LANE sheds to its metered twin (ok + shed + held) — the concurrent-fan case;
  - a saturated METERED vendor (no cheaper twin) is an honest NOT-ok (no shed);
  - no_metered_fallback → a saturated lane is NOT-ok (never a surprise metered charge);
  - deadline<=0 / governor-off → ungoverned admit (ok, not held), release() a no-op.

Hermetic: in-process governor only (XP_OFF), adapters._lane_for stubbed — no network."""
import os
import sys
import tempfile

os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="spendguard-admit-")
os.environ.setdefault("SPENDGUARD_TEST_ISOLATED", "1")
os.environ.setdefault("SPENDGUARD_NO_AUTOINSTALL", "1")
os.environ["SPENDGUARD_DISPATCH_XP_OFF"] = "1"
os.environ["SPENDGUARD_DISPATCH_LANE_CONCURRENCY"] = "1"       # 1 lane slot → one held acquire saturates it
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from spendguard import dispatch, adapters   # noqa: E402

fails = []


def ck(name, cond):
    print(("  [OK] " if cond else "  [FAIL] ") + name)
    if not cond:
        fails.append(name)


adapters._lane_for = lambda prov: ("claude-code", object()) if prov == "anthropic" else None   # anthropic rides a lane

print("-- a free $0 lane admits: held, not shed --")
a = dispatch.admit("anthropic", "m", deadline_s=5)
ck("free lane → ok + held + not shed", a.ok and a.held and not a.shed)
a.release()

print("\n-- a SATURATED $0 lane sheds to its metered twin (the concurrent-fan case) --")
dispatch.acquire("anthropic", "m", deadline_s=10)             # hold the only claude-code lane slot
try:
    b = dispatch.admit("anthropic", "m", deadline_s=0.6)
    ck("saturated $0 lane → ok + SHED + held (run metered_only, not a hard deadline)", b.ok and b.shed and b.held)
    b.release()
finally:
    dispatch.release("anthropic", "m")

print("\n-- a saturated METERED vendor has no cheaper twin → honest NOT-ok --")
os.environ["SPENDGUARD_DISPATCH_VENDOR_CONCURRENCY"] = "1"
dispatch.acquire("openai", "m", deadline_s=10)                # openai is not a lane → vendor key
try:
    c = dispatch.admit("openai", "m", deadline_s=0.5)
    ck("saturated metered vendor → NOT ok, no shed, carries the deadline reason", (not c.ok) and (not c.shed) and bool(c.error))
finally:
    dispatch.release("openai", "m")
    os.environ.pop("SPENDGUARD_DISPATCH_VENDOR_CONCURRENCY", None)

print("\n-- no_metered_fallback: a saturated lane is an honest deadline, never a surprise metered charge --")
dispatch.acquire("anthropic", "m", deadline_s=10)
try:
    d = dispatch.admit("anthropic", "m", deadline_s=0.6, no_metered_fallback=True)
    ck("no_metered_fallback → saturated lane NOT ok (no shed)", (not d.ok) and (not d.shed))
finally:
    dispatch.release("anthropic", "m")

print("\n-- deadline<=0 → ungoverned admit (proceed, nothing held); release() is a no-op --")
e = dispatch.admit("anthropic", "m", deadline_s=0)
ck("deadline<=0 → ok + NOT held (ungoverned)", e.ok and not e.held)
e.release()

print(f"\n{'[FAIL]' if fails else 'OK'} test_governed_admission: {len(fails)} failure(s)")
sys.exit(1 if fails else 0)

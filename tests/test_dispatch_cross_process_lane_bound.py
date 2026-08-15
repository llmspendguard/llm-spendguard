"""Cross-process lane admission: two SEPARATE processes on one subscription lane are co-governed by flock
slot-files, so lane calls across processes queue instead of all hitting the one plan at once.

The in-process semaphores cannot see another interpreter — but two `spendguard ask` runs, or a panel plus a
honestreview run, still share ONE Max / Codex / GLM plan per lane. This pins the file-lock layer that closes
that gap: a helper subprocess holds the single slot of a 1-slot lane; this process must then TIME OUT trying to
take the same slot, and SUCCEED once the helper releases (proving the OS frees the flock — no stale lock).

Offline, isolated home. Skips cleanly where fcntl is unavailable (non-POSIX), because the guarantee is too.
"""
import os
import sys
import tempfile
import time
import subprocess
import textwrap

if not os.environ.get("SPENDGUARD_TEST_ISOLATED"):
    os.environ["SPENDGUARD_TEST_ISOLATED"] = "1"
    os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="spendguard-xp-")
    os.execv(sys.executable, [sys.executable] + sys.argv)

from spendguard import dispatch   # noqa: E402

fails = 0


def ck(label, cond, extra=""):
    global fails
    if not cond:
        fails += 1
    print(f"  [{'OK' if cond else 'FAIL'}] {label}{('  — ' + extra) if extra and not cond else ''}")


if dispatch._xp_off():
    print("  [SKIP] fcntl unavailable / XP off — cross-process gating not supported here (in-process bound still holds)")
    print("\nOK test_dispatch_cross_process_lane_bound: 0 failure(s)")
    sys.exit(0)

KEY = "lane:testplan"
HOME = os.environ["SPENDGUARD_HOME"]
held_flag = os.path.join(HOME, "helper_held")
release_flag = os.path.join(HOME, "helper_release")

# A helper PROCESS that grabs the single slot, signals HELD, waits for the release flag, then frees it. Sharing
# SPENDGUARD_HOME means it shares the slot dir, so its flock and ours coordinate across the process boundary.
helper_src = textwrap.dedent(f"""
    import os, time
    from spendguard import dispatch
    slot = dispatch._acquire_xp({KEY!r}, 1, 10)
    open({held_flag!r}, 'w').close()
    for _ in range(200):
        if os.path.exists({release_flag!r}):
            break
        time.sleep(0.05)
    slot.release()
""")

proc = subprocess.Popen([sys.executable, "-c", helper_src], env=dict(os.environ))
for _ in range(200):
    if os.path.exists(held_flag):
        break
    time.sleep(0.05)
ck("helper subprocess acquired the single cross-process slot", os.path.exists(held_flag))

timed_out = False
try:
    dispatch._acquire_xp(KEY, 1, 0.6)                 # the one slot is held by the OTHER process
except dispatch.DispatchTimeout:
    timed_out = True
ck("a second PROCESS cannot take the held lane slot — it times out (co-governed across processes)", timed_out)

open(release_flag, "w").close()
proc.wait(timeout=10)
acquired, got = False, None
try:
    got = dispatch._acquire_xp(KEY, 1, 3)            # helper released → slot must be free (no stale lock)
    acquired = True
finally:
    if got is not None:
        got.release()
ck("once the other process releases, the slot is free again (the OS drops the flock — no stale lock)", acquired)

print(f"\n{'[FAIL]' if fails else 'OK'} test_dispatch_cross_process_lane_bound: {fails} failure(s)")
sys.exit(1 if fails else 0)

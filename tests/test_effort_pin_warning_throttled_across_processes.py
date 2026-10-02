"""The unhonored-effort-pin warning is throttled ACROSS processes, not just within one.

Regression for caller-feedback #9: `[bulkgate] EFFORT PIN NOT HONORED …` fired on ~EVERY hook invocation all
session. The announce gate was the in-process decade counter (`_TRUNC_ANNOUNCE`), but each hook is a FRESH
interpreter, so the count always reset to 1 — which is a decade boundary — and the line printed every single call.
A not-honored effort pin is a STANDING CONFIG fact (equally true on call 1 and call 10,000), so its announce is now
throttled by WALL-CLOCK across processes via config state persisted under SPENDGUARD_HOME.

Here "a fresh process" is simulated by clearing the in-process count dict between calls (so `n` recomputes to 1,
exactly as a new interpreter would) while the persisted throttle file in the shared SPENDGUARD_HOME survives — which
is precisely the hook topology. Offline, isolated home, zero spend.
"""
import os
import sys
import io
import tempfile
import contextlib

if not os.environ.get("SPENDGUARD_TEST_ISOLATED"):
    os.environ["SPENDGUARD_TEST_ISOLATED"] = "1"
    os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="sg-effortwarn-")
    os.execv(sys.executable, [sys.executable] + sys.argv)

from spendguard import bulkgate, config   # noqa: E402

class Checks:
    """Accumulates failures on its OWN instance (self), so there is no module-level mutable counter."""
    def __init__(self):
        self.fails = []

    def __call__(self, label, cond, extra=""):
        if not cond:
            self.fails.append(label)
        print(f"  [{'OK' if cond else 'FAIL'}] {label}{('  — ' + extra) if extra and not cond else ''}")


ck = Checks()


MARK = "EFFORT PIN NOT HONORED"


def announce_output(model, requested, chosen, fresh_process=False):
    """Call note_unhonored_effort and return what it wrote to stderr. `fresh_process=True` clears the in-process
    count dict first, reproducing a brand-new interpreter whose count starts at 0→1 (the hook case)."""
    if fresh_process:
        with bulkgate._unhonored_lock:
            bulkgate._unhonored_effort.clear()
    buf = io.StringIO()
    with contextlib.redirect_stderr(buf):
        bulkgate.note_unhonored_effort(model, requested, chosen)
    return buf.getvalue()


# ── 1. THE regression: a second FRESH process with the same pin stays quiet (old code printed both) ──────────────
first = announce_output("gpt-5.6-sol", "minimal", "none", fresh_process=True)
ck("first occurrence in the window ANNOUNCES", MARK in first)
second = announce_output("gpt-5.6-sol", "minimal", "none", fresh_process=True)
ck("second FRESH process (count back to 1) is SUPPRESSED by the cross-process throttle", MARK not in second,
   extra=f"re-announced despite the persisted throttle: {second!r}")

# ── 2. a DIFFERENT (model|req->chosen) key still announces in the same window (keys are independent) ─────────────
other = announce_output("gpt-5.7-ultra", "minimal", "none", fresh_process=True)
ck("a different pin key is NOT suppressed by another key's throttle", MARK in other)

# ── 3. the RECORD is unaffected: every event is still counted (unhonored_efforts sees them), throttle or not ─────
with bulkgate._unhonored_lock:
    bulkgate._unhonored_effort.clear()
for _ in range(3):
    announce_output("gpt-5.6-rec", "minimal", "none")   # same process, no clear → count accumulates
rec = bulkgate.unhonored_efforts().get("gpt-5.6-rec|minimal->none")
ck("every event is recorded even while the announce is throttled", rec == 3, extra=f"count={rec!r}")

# ── 4. the window is real: SPENDGUARD_EFFORT_WARN_THROTTLE_S=0 makes every call due again ────────────────────────
os.environ["SPENDGUARD_EFFORT_WARN_THROTTLE_S"] = "0"
try:
    a = announce_output("gpt-5.6-win", "minimal", "none", fresh_process=True)
    b = announce_output("gpt-5.6-win", "minimal", "none", fresh_process=True)
    ck("throttle window of 0s re-announces every call (env override honored)", MARK in a and MARK in b)
finally:
    del os.environ["SPENDGUARD_EFFORT_WARN_THROTTLE_S"]

# ── 5. the throttle PERSISTED to SPENDGUARD_HOME (a real cross-process store, not a module global) ───────────────
state_file = config.state_path(bulkgate._EFFORT_WARN_STATE)
ck("throttle state is written under SPENDGUARD_HOME", state_file.exists(),
   extra=f"missing {state_file}")
ck("persisted state carries the throttled key", "gpt-5.6-sol|minimal->none" in config.load_state(bulkgate._EFFORT_WARN_STATE, {}))

# ── 6. degrade-on-failure: if the cross-process store is unreadable, fall back to the in-process first-occurrence
#       gate (n==1), never silencing the guardrail nor re-spamming a loop ─────────────────────────────────────────
_orig_load = config.load_state
try:
    config.load_state = lambda *a, **k: (_ for _ in ()).throw(OSError("state unreadable"))
    ck("on state-read failure, first in-process occurrence (n==1) is still due", bulkgate._effort_pin_warn_due("k|x->y", 1) is True)
    ck("on state-read failure, a later in-process occurrence (n>1) is suppressed", bulkgate._effort_pin_warn_due("k|x->y", 5) is False)
finally:
    config.load_state = _orig_load

# ── 7. never raises (the announce path is best-effort; a broken stderr must not lose the recorded event) ─────────
try:
    with bulkgate._unhonored_lock:
        bulkgate._unhonored_effort.clear()
    bulkgate.note_unhonored_effort("gpt-5.6-noraise", "minimal", "none")
    ck("note_unhonored_effort never raises", True)
except Exception as e:  # noqa: BLE001
    ck("note_unhonored_effort never raises", False, extra=repr(e))

print(f"\n{'OK' if not ck.fails else 'FAIL'} test_effort_pin_warning_throttled_across_processes: {len(ck.fails)} failure(s)")
sys.exit(1 if ck.fails else 0)

"""The bulk gate (estimate→test→eval) must REACH the lane path, not only the patched-SDK path (warden Q8, axis-4).

A lane executes a CLI subprocess — it touches no patched SDK, so gate._bulkgate_check never fires and a lane fan was
otherwise UNGOVERNED. The spend cap is moot ($0 on a plan), but "was this proven to work before N thousand ran?" is
not about dollars. bulk_delegate now applies the SAME enforce-mode gate the SDK path gets: a genuinely-bulk fan
(>= preview_max) whose intent was not gated (estimate→test→eval, verified via gate_sig) is refused under
SPENDGUARD_ENFORCE=block, logged 'would-block' under warn, allowed under off. Offline: adapters.call/dispatch stubbed.
"""
import os
import sys
import tempfile

os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="sg-gatereach-")
os.environ.setdefault("SPENDGUARD_TEST_ISOLATED", "1")
os.environ.setdefault("SPENDGUARD_NO_AUTOINSTALL", "1")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from spendguard import lane_balance, lane_catalog, lane_bandit, adapters, dispatch, lane_economics, bulkgate as bg, gate as sg

fails = []


def ck(name, cond):
    print(("  [OK] " if cond else "  [FAIL] ") + name)
    if not cond:
        fails.append(name)


# a lane world so the tasks WOULD run if allowed
lane_catalog.arms = lambda flt=None: [("gemini", "g-high")]
lane_catalog.lane_provider = lambda l: {"gemini": "gemini"}.get(l)
lane_bandit._arm_cooling = lambda l, u: False
lane_bandit.arm_stats = lambda intent: {("gemini", "g-high"): {"winrate": 1.0, "trials": 2}}
lane_economics.prompt_lane_reserved = lambda lane: False
adapters._lane_cooling = lambda ln: False
dispatch.acquire = lambda *a, **k: 0.0
dispatch.release = lambda *a, **k: None
adapters.call = lambda model, prompt, **kw: {"text": "ok", "cost": 0}

H = os.environ["SPENDGUARD_HOME"]
TASKS = ["t%d" % i for i in range(30)]                    # >= preview_max (25) → a genuinely-bulk fan


def _run(intent, name, **kw):
    return lane_balance.bulk_delegate(TASKS, intent, checkpoint=os.path.join(H, name), chunk_size=5, **kw)


# ── enforce=block + NOT gated → REFUSED (the fan was never estimate/test/eval'd) ──
os.environ["SPENDGUARD_ENFORCE"] = "block"
raised = None
try:
    _run("warden:ungated", "a.jsonl")
except bg.GateBlocked as e:
    raised = e
ck("enforce=block: an UNGATED bulk lane fan is REFUSED (GateBlocked)", raised is not None and "REFUSED" in str(raised))
ck("...GateBlocked is a DELIBERATE stop (fail-open handlers re-raise it, never swallow)",
   isinstance(raised, sg.deliberate_stop_types()))

# ── enforce=warn (the roll-out default): logged 'would-block' but ALLOWED to run ──
os.environ["SPENDGUARD_ENFORCE"] = "warn"
res = _run("warden:ungated", "b.jsonl")
ck("enforce=warn: an ungated bulk fan is ALLOWED (would-block logged, not refused)",
   len(res) == 30 and all(r.get("text") for r in res))

# ── enforce=block + a FRESH gate_sig (estimate+test+eval passing) → ALLOWED ──
os.environ["SPENDGUARD_ENFORCE"] = "block"
_orig_gs = bg.gate_status
bg.gate_status = lambda s, **k: {"fresh": True, "reason": ""} if s == "GATED" else _orig_gs(s, **k)
res2 = _run("warden:gated", "c.jsonl", gate_sig="GATED")
bg.gate_status = _orig_gs
ck("enforce=block: a fan WITH a fresh gate_sig is ALLOWED (the discipline was met)",
   len(res2) == 30 and all(r.get("text") for r in res2))

# ── force=True overrides the gate (caller owns the risk) ──
res3 = _run("warden:ungated", "d.jsonl", force=True)
ck("force=True overrides the bulk gate (caller owns the risk)", len(res3) == 30 and all(r.get("text") for r in res3))

# ── a SMALL fan (< preview_max) is not bulk → the gate does not apply even under block ──
res4 = lane_balance.bulk_delegate(["one", "two"], "warden:small")
ck("a small fan (< preview_max) is not bulk-gated", len(res4) == 2 and all(r.get("text") for r in res4))
del os.environ["SPENDGUARD_ENFORCE"]

print(("[OK]" if not fails else "[FAIL]") + " bulk gate reach: %d failure(s)" % len(fails))
sys.exit(1 if fails else 0)

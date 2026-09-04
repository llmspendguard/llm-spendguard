"""bulk_delegate(lanes=…) confines a fan to an explicit lane subset — so fungible bulk can exclude a protected
interactive lane (claude-code) WITHOUT mutating advisor.delegate_lanes.

Grounding: _bulk_arms already accepts lanes=; bulk_delegate now forwards it (default path AND the tier= path). The
guard is that a confined fan touches ONLY the named lanes, and a lane NOT named never receives a task. Offline:
lane_catalog.arms honors its filter arg, adapters.call records which model it saw, no LLM.
"""
import os
import sys
import tempfile

os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="sg-laneconf-")
os.environ.setdefault("SPENDGUARD_TEST_ISOLATED", "1")
os.environ.setdefault("SPENDGUARD_NO_AUTOINSTALL", "1")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from spendguard import lane_balance, lane_catalog, lane_bandit, adapters, dispatch, lane_economics

fails = []


def ck(name, cond):
    print(("  [OK] " if cond else "  [FAIL] ") + name)
    if not cond:
        fails.append(name)


# a 3-lane world; lane_catalog.arms HONORS its filter arg (so lanes= actually narrows the set)
ALL = ["codex", "gemini", "zai-coding"]
lane_catalog.arms = lambda flt=None: [(l, l + "-m") for l in (flt or ALL)]
lane_catalog.lane_provider = lambda l: l
lane_bandit._arm_cooling = lambda l, u: False
lane_bandit.arm_stats = lambda intent: {}
lane_economics.prompt_lane_reserved = lambda lane: False
adapters._lane_cooling = lambda ln: False
dispatch.acquire = lambda *a, **k: 0.0
dispatch.release = lambda *a, **k: None

_seen_models = []


def _fake_call(model, prompt, **kw):
    _seen_models.append(model)
    return {"text": "ok", "cost": 0, "executor": model.split(":")[0], "model": model.split(":")[1], "provider": model.split(":")[0]}


adapters.call = _fake_call

TASKS = ["t%d" % i for i in range(12)]

# ── unconfined: all three lanes serve (round-robin) ──
_seen_models.clear()
r0 = lane_balance.bulk_delegate(TASKS, "conf:none")
lanes0 = {m.split(":")[0] for m in _seen_models}
ck("unconfined fan uses all three lanes", lanes0 == set(ALL) and len(r0) == 12)

# ── confined to a subset: ONLY those lanes serve, the excluded lane gets NOTHING ──
_seen_models.clear()
r1 = lane_balance.bulk_delegate(TASKS, "conf:sub", lanes=["gemini", "zai-coding"])
lanes1 = {m.split(":")[0] for m in _seen_models}
ck("lanes=[gemini,zai] → only those two serve", lanes1 == {"gemini", "zai-coding"})
ck("...the excluded lane (codex) received ZERO tasks", "codex" not in lanes1 and len(r1) == 12)

# ── confined to ONE lane: every task lands there ──
_seen_models.clear()
r2 = lane_balance.bulk_delegate(TASKS, "conf:one", lanes=["gemini"])
lanes2 = {m.split(":")[0] for m in _seen_models}
ck("lanes=[gemini] → every task lands on gemini", lanes2 == {"gemini"} and len(_seen_models) == 12)

# ── a named lane that is RESERVED still drops out (fail-closed, never widened past) ──
lane_economics.prompt_lane_reserved = lambda lane: lane == "gemini"
r3 = lane_balance.bulk_delegate(TASKS, "conf:reserved", lanes=["gemini", "codex"])
served3 = {row["lane"] for row in r3 if row.get("text")}
ck("a reserved lane named in lanes= is still excluded (fail-closed)", "gemini" not in served3 and served3 == {"codex"})
lane_economics.prompt_lane_reserved = lambda lane: False

print(("[OK]" if not fails else "[FAIL]") + " bulk lane confinement: %d failure(s)" % len(fails))
sys.exit(1 if fails else 0)

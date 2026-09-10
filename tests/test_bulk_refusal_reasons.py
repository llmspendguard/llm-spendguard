"""bulk_delegate's fan-wide REFUSAL rows must carry a structured `reason` — and a PERMANENT config gap must be
distinguishable from a TRANSIENT capacity one. warden aggregates misses by `row.get("reason")`; the refusal rows
used to carry only `error`, so 26 identical config-gap refusals logged as `{'no_row': 26}` — shaped exactly like a
transient blip — and the inertness went unnoticed for weeks.

Locked here: every fan-wide refusal returns one row per task carrying a `reason` code, and the codes SEPARATE a
config gap (tier_undeclared / no_viable_lane — refuses every run) from a transient condition (all_lanes_cooling /
all_lanes_reserved — retry later). Offline: the lane catalog / cooling / arms / reservation are stubbed — no fan runs.
"""
import os
import sys
import tempfile

os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="sg-bulkreason-")
os.environ.setdefault("SPENDGUARD_TEST_ISOLATED", "1")
os.environ.setdefault("SPENDGUARD_NO_AUTOINSTALL", "1")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from spendguard import lane_balance, lane_catalog, adapters, lane_economics      # noqa: E402

fails = []


def ck(name, cond):
    print(("  [OK] " if cond else "  [FAIL] ") + name)
    if not cond:
        fails.append(name)


TASKS = ["t1", "t2"]


def _reasons(rows):
    return {r.get("reason") for r in rows}


# ── TIER, undeclared group (PERMANENT): no lane declares a model for it → tier_undeclared ──
lane_catalog.lanes = lambda: ["a", "b"]
lane_catalog.lane_model_for_tier = lambda ln, t: None          # nothing serves the group
adapters._lane_cooling = lambda ln: False
rows = lane_balance.bulk_delegate(TASKS, "intent", tier="cheap", force=True)
ck("undeclared tier → a row per task, all reason=tier_undeclared (a config gap, not a blip)",
   len(rows) == len(TASKS) and _reasons(rows) == {"tier_undeclared"})

# ── TIER, declared but every serving lane cooling (TRANSIENT): all_lanes_cooling ──
lane_catalog.lane_model_for_tier = lambda ln, t: "some-model"  # the group IS declared on lanes…
adapters._lane_cooling = lambda ln: True                       # …but all of them are cooling right now
rows = lane_balance.bulk_delegate(TASKS, "intent", tier="cheap", force=True)
ck("declared-but-all-cooling → reason=all_lanes_cooling (transient, DISTINCT from undeclared)",
   _reasons(rows) == {"all_lanes_cooling"})

# ── NO tier, no viable lane (config): no_viable_lane ──
lane_balance._bulk_arms = lambda intent, lanes=None: []
rows = lane_balance.bulk_delegate(TASKS, "intent", force=True)
ck("no tier + empty arms → reason=no_viable_lane", _reasons(rows) == {"no_viable_lane"})

# ── NO tier, arms exist but all reserved (TRANSIENT): all_lanes_reserved ──
lane_balance._bulk_arms = lambda intent, lanes=None: [("a", "m")]
lane_economics.prompt_lane_reserved = lambda ln: True
rows = lane_balance.bulk_delegate(TASKS, "intent", force=True)
ck("arms all reserved → reason=all_lanes_reserved (transient)", _reasons(rows) == {"all_lanes_reserved"})

# ── every refusal row still carries the human error string too (reason is ADDED, not a replacement) ──
ck("a refusal row carries BOTH reason and a human-readable error", all(r.get("reason") and r.get("error") for r in rows))

print(("[OK]" if not fails else "[FAIL]") + " bulk refusal reasons: %d failure(s)" % len(fails))
sys.exit(1 if fails else 0)

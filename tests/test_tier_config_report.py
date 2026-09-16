"""tier_config_report — the bulk-lane surface's config health that `spendguard doctor` + `spendguard tiers` render.

The 2026-09-10 warden report: an UNSET advisor.tiers makes bulk_delegate refuse EVERY `--tier` fan while idle $0
lanes sit at 0.00x — a capability built, wired, and INERT, which is worse than absent because the code looks
protected. This locks the VISIBILITY the fix depends on, so the silent-inertness state can never ship unnoticed
again: configured=False when tiers are unset (doctor renders NOT CONFIGURED), plus the two declaration-time
validations the CLI relies on — a group model must be PRICED, and a declared group that no lane serves is a hard
issue (the exact inert state, reason=tier_undeclared at runtime).

Pure logic over INJECTED config — no live lanes, no network, no spend, no pricing table dependency.
"""
import os
import sys
import tempfile

if not os.environ.get("SPENDGUARD_TEST_ISOLATED"):
    os.environ["SPENDGUARD_TEST_ISOLATED"] = "1"
    os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="spendguard-tiercfg-")
    os.execv(sys.executable, [sys.executable] + sys.argv)

from spendguard import tier_config, route_utility, config, lane_catalog, lane_balance   # noqa: E402

fails = []


def ck(name, cond):
    print(("  [OK] " if cond else "  [FAIL] ") + name)
    if not cond:
        fails.append(name)


_orig_cfg_get = config._cfg_get


def report(tiers, lane_models, priced, serves, base, idle=()):
    """Drive tier_config_report over injected config so the test exercises its LOGIC (groups→lane→priced→issues),
    not the live lane/pricing environment. `serves` = {group: [lanes that serve it]}; `priced` = the priced set."""
    route_utility.tiers = lambda: dict(tiers)                     # advisor.tiers (route_utility owns the read)
    config._cfg_get = lambda s, k, d=None: (dict(lane_models) if (s, k) == ("advisor", "lane_models")
                                            else _orig_cfg_get(s, k, d))
    tier_config._model_is_priced = lambda m: m in priced
    lane_catalog.lanes = lambda: list(base.keys())
    lane_catalog.lane_model_for_tier = lambda ln, g: ln in serves.get(g, [])
    lane_catalog.configured_base = lambda ln: base.get(ln)
    lane_balance.idle_lanes = lambda: list(idle)
    return tier_config.tier_config_report()


print("-- UNSET advisor.tiers → configured False (doctor renders 🔴 NOT CONFIGURED) --")
r = report({}, {}, set(), {}, {}, idle=["codex", "gemini"])
ck("advisor.tiers unset → configured is False", r["configured"] is False)
ck("idle lane capacity is surfaced so the fix message can name it", r["idle"] == ["codex", "gemini"])

print("-- DECLARED + PRICED + a lane serves → configured, group green, no hard issue --")
r = report({"cheap": ["m-cheap"]}, {"codex": "m-cheap"}, {"m-cheap"}, {"cheap": ["codex"]}, {"codex": "m-cheap"})
ck("declared → configured True", r["configured"] is True)
ck("the group lists its serving lane, nothing unpriced, and there is no hard issue",
   r["groups"]["cheap"]["lanes"] == ["codex"] and r["groups"]["cheap"]["unpriced"] == [] and r["issues"] == [])

print("-- an UNPRICED group model is CAUGHT (the declaration-time cost-visibility validation) --")
r = report({"cheap": ["m-nope"]}, {"codex": "m-nope"}, set(), {"cheap": ["codex"]}, {"codex": "m-nope"})
ck("the unpriced model is surfaced in groups[g]['unpriced'] and raises a hard issue",
   r["groups"]["cheap"]["unpriced"] == ["m-nope"] and len(r["issues"]) >= 1)

print("-- DECLARED but NO lane serves it → the exact INERT state, made a hard issue (a --tier fan would refuse) --")
r = report({"cheap": ["m-cheap"]}, {"codex": "m-cheap"}, {"m-cheap"}, {}, {"codex": "m-cheap"})
ck("a group no lane serves → 0 serving lanes AND a hard issue (not a silent inert pass)",
   r["groups"]["cheap"]["lanes"] == [] and len(r["issues"]) >= 1)

print(f"\n{'[FAIL]' if fails else 'OK'} test_tier_config_report: {len(fails)} failure(s)")
sys.exit(1 if fails else 0)

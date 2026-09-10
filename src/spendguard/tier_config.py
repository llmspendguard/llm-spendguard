"""Declare + VALIDATE the bulk-lane routing config — the surface that decides whether bulk_delegate can serve.

advisor.tiers (capability GROUPS: {group: [models]}) and advisor.lane_models (each lane's model, plain or per-tier)
are what make bulk_delegate route to $0 subscription lanes instead of the metered Batch API. Unset or half-declared,
a `--tier` fan refuses EVERY task (reason=tier_undeclared) while lanes sit idle — a capability that is built, wired,
and inert, which is worse than absent because the code looks protected. This module makes that state VISIBLE (doctor
+ `spendguard tiers`) and DECLARABLE with validation at the moment of declaration (each model priced; each lane's
model in a group), turning a silent misconfiguration into an error when someone is actually paying attention.

route_utility owns the ROUTING (rank_for_tier); this module owns the DECLARATION + validation + CLI.
"""
from . import config, lane_catalog, route_utility


def _model_is_priced(model):
    """True iff pricing.py can price this model id. A group/lane model MUST be priced — an unpriced model means its
    $0-lane VALUE and its metered fallback both go uncosted, the silent misconfiguration this surface makes loud.
    An UNPRICED model surfaces as pricing.price's KeyError → False; a deliberate stop (spend refusal / deadline) is
    NEVER swallowed into 'unpriced' — it propagates, so a governance signal can't be downgraded to 'keep going'."""
    if not model:
        return False
    from . import pricing, gate
    try:
        pricing.price(model)
        return True
    except gate.deliberate_stop_types():
        raise                                            # a refusal/deadline is not an 'unpriced' verdict — it HALTS
    except Exception:
        return False                                     # KeyError (no canonical price) and the like → not priced


def _write_advisor_cfg(key, value):
    """Persist advisor.<key> to config.json, preserving the rest of the [advisor] section, then invalidate the cache
    so the very next read sees it (a `config set` followed by a read in the same process used to see the stale value)."""
    def _mut(d):
        d = dict(d or {})
        adv = dict(d.get("advisor") or {})
        if value is None:
            adv.pop(key, None)
        else:
            adv[key] = value
        d["advisor"] = adv
        return d
    config.update_json(config.CONFIG_JSON, _mut, reason=f"set advisor.{key}")
    config.cfg_invalidate()


def tier_config_report():
    """The bulk-lane surface's config health, as data (doctor + the `tiers` CLI both render this). Answers the two
    questions the review named: is each declared GROUP model priced, and does each group have a LANE serving it; and
    is each lane's declared model IN some group (else it can never be picked for a --tier fan). issues = hard gaps
    (a fan will refuse / mis-cost); warnings = a lane that serves no group. configured=False → advisor.tiers unset."""
    from . import lane_balance
    groups = route_utility.tiers()                       # the SAME advisor.tiers key route_utility routes on
    lane_models = config._cfg_get("advisor", "lane_models", {}) or {}
    issues, warns, gout, lout = [], [], {}, {}
    for g, models in groups.items():
        unpriced = [m for m in models if not _model_is_priced(m)]
        served_by = [ln for ln in lane_catalog.lanes() if lane_catalog.lane_model_for_tier(ln, g)]
        gout[g] = {"models": list(models), "unpriced": unpriced, "lanes": served_by}
        if unpriced:
            issues.append(f"group {g!r}: unpriced model(s) {', '.join(unpriced)} — `spendguard sync-prices` or add to prices.json")
        if not served_by:
            issues.append(f"group {g!r}: declared but NO lane serves it → a `--tier {g}` fan refuses (reason=tier_undeclared)")
    for ln, decl in lane_models.items():
        base = lane_catalog.configured_base(ln)
        in_groups = [g for g in groups if base in groups.get(g, [])] if base else []
        priced = _model_is_priced(base) if base else False
        lout[ln] = {"base": base, "in_groups": in_groups, "priced": priced, "per_tier": isinstance(decl, dict)}
        if base and not priced:
            issues.append(f"lane {ln!r}: base model {base!r} is not priced")
        if base and not in_groups and not isinstance(decl, dict):
            warns.append(f"lane {ln!r}: its model {base!r} is in no advisor.tiers group → serves no `--tier` fan (only bare bulk)")
    try:
        idle = lane_balance.idle_lanes()
    except Exception:
        idle = []
    return {"configured": bool(groups), "groups": gout, "lane_models": lout,
            "issues": issues, "warnings": warns, "idle": idle}


def main(argv=None):
    """`spendguard tiers` — show + VALIDATE the bulk-lane routing groups; `tiers set <group> <model…>` declares one,
    refusing an unpriced model at the moment of declaration (the only moment anyone is watching)."""
    argv = list(argv or [])
    if argv and argv[0] == "set":
        if len(argv) < 3:
            print("usage: spendguard tiers set <group> <model> [<model> …]   (the models a `--tier <group>` fan may use)")
            print("  then map a lane to one of them: `spendguard lanes set-model <lane> <model>`")
            return 2
        group, models = argv[1], argv[2:]
        unpriced = [m for m in models if not _model_is_priced(m)]
        if unpriced:
            print(f"refusing to declare {group!r}: unpriced model(s) {', '.join(unpriced)} — a group model must be "
                  f"priced (so its fan is cost-visible). `spendguard sync-prices`, or add them to prices.json WITH A "
                  f"SOURCE, then re-run.")
            return 2
        _write_advisor_cfg("tiers", {**route_utility.tiers(), group: list(models)})
        print(f"advisor.tiers[{group!r}] = {list(models)}   → {config.CONFIG_JSON}")
        served = [ln for ln in lane_catalog.lanes() if lane_catalog.lane_model_for_tier(ln, group)]
        if not served:
            print(f"  ⚠ no lane serves {group!r} yet → a `--tier {group}` fan still refuses (reason=tier_undeclared). "
                  f"Map one: `spendguard lanes set-model <lane> <a model in this group>`.")
        else:
            print(f"  served by lane(s): {', '.join(served)}")
        return 0
    if argv and argv[0] not in ("show", "list"):
        print(f"unknown tiers subcommand {argv[0]!r} — `tiers` (show + validate) or `tiers set <group> <model…>`")
        return 2
    rep = tier_config_report()
    if not rep["configured"]:
        print("bulk-lane routing groups (advisor.tiers): 🔴 NOT CONFIGURED — a `--tier` bulk_delegate fan refuses every task.")
        if rep["idle"]:
            print(f"  idle lane capacity available now: {', '.join(rep['idle'])}")
        print("  declare one: `spendguard tiers set cheap <model…>` then `spendguard lanes set-model <lane> <model>`")
        return 0
    print("bulk-lane routing groups (advisor.tiers):")
    for g, d in rep["groups"].items():
        flag = "🟢" if d["lanes"] and not d["unpriced"] else "🔴"
        print(f"  {flag} {g:<8} {len(d['lanes'])} lane(s): {', '.join(d['lanes']) or '(none — this fan refuses)'}"
              + (f"   UNPRICED: {', '.join(d['unpriced'])}" if d["unpriced"] else ""))
    for ln, d in rep["lane_models"].items():
        tag = f"groups: {', '.join(d['in_groups'])}" if d["in_groups"] else ("per-tier map" if d["per_tier"] else "⚠ in no group")
        print(f"  lane {ln:<12} → {(d['base'] or '(unset)'):<24} {tag}")
    for i in rep["issues"]:
        print(f"  🔴 {i}")
    for w in rep["warnings"]:
        print(f"  🟡 {w}")
    print("  change: `spendguard tiers set <group> <model…>` · `spendguard lanes set-model <lane> <model>`")
    return 0 if not rep["issues"] else 1

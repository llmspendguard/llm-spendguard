"""Declare + VALIDATE the bulk-lane routing config — the surface that decides whether bulk_delegate can serve.

advisor.tiers (capability GROUPS: {group: [models]}) and advisor.lane_models (each lane's model, plain or per-tier)
are what make bulk_delegate route to $0 subscription lanes instead of the metered Batch API. Unset or half-declared,
a `--tier` fan refuses EVERY task (reason=tier_undeclared) while lanes sit idle — a capability that is built, wired,
and inert, which is worse than absent because the code looks protected. This module makes that state VISIBLE (doctor
+ `spendguard tiers`) and DECLARABLE with validation at the moment of declaration (each model priced; each lane's
model in a group), turning a silent misconfiguration into an error when someone is actually paying attention.

route_utility owns the ROUTING (rank_for_tier); this module owns the DECLARATION + validation + CLI.
"""
import time

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


_REACH_PROMPT = "Reply with exactly: OK"     # a trivial served-check; only accept/serve matters, not the content
_REACH_TIMEOUT_S = 45                        # a lane's real latency is seconds; a hung CLI fails fast, never stalls
_REACH_SNAPSHOT = "tier_reachability"        # persisted so doctor/tiers render the last probe without re-hitting CLIs


def reachability_probe(save=True):
    """The check tier_config_report CANNOT make: does each ENABLED lane's DECLARED tier model actually SERVE $0 on
    its lane, or does the lane CLI REJECT it and silently fall back to the METERED API (the 'green report, inert
    lane' class, one level below tier_config_report)? Dispatches through adapters.call — the SAME production path,
    so the lane-boundary resolution (e.g. the Gemini composer that turns a base id into a served tier suffix) is
    applied exactly as a real fungible call would be. PINNED (no_substitution) + no_metered_fallback => $0 BY
    CONSTRUCTION: a lane miss is an error, never a metered call. served = the lane answered (text, no error).
    Persists a snapshot (save) so doctor/tiers render it without re-probing. Returns
    {asof, rows:[{lane, model, tiers, served, error}]}."""
    from . import adapters, gate, lanes
    groups = route_utility.tiers()
    enabled = {ln["lane"] for ln in lanes.lanes_status()["lanes"] if ln["enabled"]}
    targets = {}                                         # (lane, model) -> the tiers that declare it (probe each once)
    for ln in lane_catalog.lanes():
        if ln not in enabled:
            continue
        for g in groups:
            model = lane_catalog.lane_model_for_tier(ln, g)
            if model:
                targets.setdefault((ln, model), []).append(g)
    rows = []
    for (ln, model), tiers in targets.items():
        prov = lane_catalog.lane_provider(ln)            # the provider namespace whose atomic pair IS this lane
        pinned = f"{prov}:{model}" if prov and ":" not in str(model) else model
        try:
            r = adapters.call(pinned, _REACH_PROMPT, no_substitution=True, no_metered_fallback=True,
                              sig="spendguard:reachability-probe", timeout_s=_REACH_TIMEOUT_S)
        except Exception as e:
            if gate.is_deliberate_stop(e):               # a deadline/refusal (DispatchTimeout, SpendGateRefused) HALTS
                raise                                    # the probe — never downgraded to a 'not served' row
            r = {"error": f"{type(e).__name__}: {str(e)[:80]}"}
        rows.append({"lane": ln, "model": model, "tiers": sorted(tiers),
                     "served": bool(r.get("text")) and not r.get("error"), "error": (r.get("error") or "")[:140]})
    snap = {"asof": time.time(), "rows": rows}
    if save:
        config.save_state(_REACH_SNAPSHOT, snap, loud=False)
    return snap


def cached_reachability():
    """The last reachability_probe snapshot as (rows, age_seconds), or (None, None) if never probed — so doctor and
    `tiers` render the DECLARED models' real serving with its age, and say 'unprobed' honestly instead of implying
    green. Read-only, $0."""
    snap = config.load_state(_REACH_SNAPSHOT, {}) or {}
    rows = snap.get("rows")
    if not rows:
        return None, None
    return rows, max(0.0, time.time() - float(snap.get("asof") or 0))


def main(argv=None):
    """`spendguard tiers` — show + VALIDATE the bulk-lane routing groups; `tiers set <group> <model…>` declares one,
    refusing an unpriced model at the moment of declaration (the only moment anyone is watching)."""
    argv = list(argv or [])
    probe = "--probe" in argv
    argv = [a for a in argv if a != "--probe"]
    if probe:
        print("probing each enabled lane's CLI for its declared model ($0: a miss is an error row, never metered)…")
        reachability_probe(save=True)
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
    # REACHABILITY: declared+priced+mapped 🟢 above is NOT proof a lane CLI accepts the id — a rejected model
    # silently meters. Render the last `--probe` verdict (the CLI's own accept/reject ground truth) so the green
    # config report can never hide an inert lane.
    _rrows, _rage = cached_reachability()
    _bad = [r for r in (_rrows or []) if not r["served"]]
    if _rrows is None:
        print("  reachability: ⚪ unprobed — `spendguard tiers --probe` verifies each lane CLI ACCEPTS its declared model ($0)")
    elif _bad:
        _ago = f"{_rage / 3600:.1f}h" if _rage >= 3600 else f"{_rage / 60:.0f}m"
        print(f"  reachability: 🔴 {len(_bad)} declared model(s) REJECTED by their lane CLI → silently meter (probed {_ago} ago):")
        for r in _bad:
            print(f"                {r['lane']} → {r['model']} [{','.join(r['tiers'])}]: {r['error'] or 'not served'}")
    else:
        _ago = f"{_rage / 3600:.1f}h" if _rage >= 3600 else f"{_rage / 60:.0f}m"
        print(f"  reachability: 🟢 every declared model served on its lane CLI (probed {_ago} ago, $0)")
    print("  change: `spendguard tiers set <group> <model…>` · `spendguard lanes set-model <lane> <model>`  ·  `--probe` re-checks serving")
    return 0 if (not rep["issues"] and not _bad) else 1

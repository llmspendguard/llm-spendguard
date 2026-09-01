"""Lane model CATALOG — the single source of truth for what each subscription LANE can invoke.

Per lane it knows: the PROVIDER (fixes vendor ambiguity in pricing), the BASE model(s), how that lane expresses
REASONING EFFORT (each provider's quirk), and therefore the actual USE-NAMES the lane calls with (e.g. the Gemini
lane's `gemini-3.7-flash-low`). Everything downstream reads THIS:

  • pricing resolves base + provider — no fragile suffix-strip, and no "gemini vs vertex_ai" ambiguity;
  • the ledger records the FULL use-name, so we see exactly what ran (reasoning level and all);
  • the lane BANDIT's arms ARE catalog entries — so it can learn which lane AND which reasoning level wins.

DERIVED, not hardcoded: the provider comes from `adapters._LANES`, the configured base from `advisor.lane_models`,
the rate from `pricing.py`. The ONLY thing defined here is each lane's reasoning QUIRK — a fixed protocol of that
lane's CLI/API, grounded in the lane execs (codex none…max, Gemini `-low/-high` suffix, zai none, claude thinking).
Add a lane in `adapters._LANES` + a `lane_models` entry and it shows up here automatically; a new reasoning style
is one edit to REASONING_QUIRK.
"""
from . import adapters, config

# HOW EACH LANE EXPRESSES REASONING EFFORT — the per-lane protocol quirk (NOT user config). Grounded in the execs:
#   • gemini (antigravity_exec): effort rides the MODEL SUFFIX  `<base>-<level>`  — the use-name carries it.
#   • codex  (codex_exec._codex_effort): a `model_reasoning_effort` param on its OWN scale — 'minimal' is rejected.
#   • claude-code (subscription_exec): the Claude CLI has no one-shot effort flag (thinking budget is separate).
#   • zai (zai_exec): none applied on this lane.
# `style="suffix"` is the only one that changes the USE-NAME; the others pass effort out-of-band, so the use-name is
# just the base. `default=None` means "leave the lane's own default".
REASONING_QUIRK = {
    "gemini":      {"style": "suffix",   "levels": ("low", "medium", "high"),                 "default": "medium"},
    "codex":       {"style": "param",    "levels": ("none", "low", "medium", "high", "xhigh", "max"), "default": None},
    "claude-code": {"style": "thinking", "levels": (),                                        "default": None},
    "zai-coding":  {"style": "none",     "levels": (),                                        "default": None},
}
_DEFAULT_QUIRK = {"style": "none", "levels": (), "default": None}


def lanes():
    """Every configured lane name (from the provider→lane registry), sorted for stable display/iteration."""
    return sorted({name for name, _mod in adapters._LANES.values()})


def lane_provider(lane):
    """The PROVIDER a lane bills against — reversed out of adapters._LANES (the one registry that owns it), so pricing
    can disambiguate a model two vendors both publish. None if the lane is not registered."""
    for prov, (name, _mod) in adapters._LANES.items():
        if name == lane:
            return prov
    return None


def quirk(lane):
    return REASONING_QUIRK.get(lane, _DEFAULT_QUIRK)


def parse_use_name(use_name, lane):
    """(base, level) for a lane's use-name. For a SUFFIX-reasoning lane, split a trailing KNOWN reasoning level (only
    those — never a blind regex); otherwise the level rides out-of-band and the whole id is the base. Provider/lane
    aware on purpose: a non-suffix lane's model that happens to end in '-high' is NEVER mis-split."""
    if not use_name:
        return use_name, None
    q = quirk(lane)
    if q["style"] == "suffix":
        for lv in q["levels"]:
            if use_name.endswith("-" + lv):
                return use_name[: -(len(lv) + 1)], lv
    return use_name, None


def configured_base(lane):
    """The lane's DEFAULT base model (advisor.lane_models[lane] with any reasoning suffix removed). None if unset.
    When the lane declares a PER-TIER map {tier: model} instead of a plain string, the default is its 'strong' entry
    (else 'default', else the first) — the highest-capability model — so every NON-tier path (the bandit, use_names,
    the catalog) keeps its prior behaviour; per-tier selection is lane_model_for_tier()."""
    m = (config._cfg_get("advisor", "lane_models", {}) or {}).get(lane)
    if isinstance(m, dict):
        m = m.get("strong") or m.get("default") or next(iter(m.values()), None)
    if not m:
        return None
    base, _lv = parse_use_name(m, lane)
    return base


def lane_model_for_tier(lane, tier):
    """The base model this lane serves for capability GROUP `tier`, or None if it serves none — the PER-TIER resolver
    that lets one plan do CHEAP work on its cheap model and STRONG work on its strong model (same $0 plan, right-sized
    per task). advisor.lane_models[lane] is EITHER a plain model string (the lane's single model — it serves `tier`
    iff that model is in advisor.tiers[tier]) OR a per-tier map {tier: model, …}, e.g.
    {"cheap": "claude-haiku-4-5", "strong": "claude-opus-4-8"}. Suffix-stripped like configured_base. A lane with no
    model for `tier` returns None (it is simply not in that group's fan-out)."""
    m = (config._cfg_get("advisor", "lane_models", {}) or {}).get(lane)
    if isinstance(m, dict):
        v = m.get(tier)
        return parse_use_name(v, lane)[0] if v else None
    if isinstance(m, str) and m:
        base, _lv = parse_use_name(m, lane)
        from . import route_utility
        return base if base in set(route_utility.tier_models(tier)) else None
    return None


def use_names(lane):
    """The actual invocable USE-NAMES for a lane: for a suffix lane, the configured base × each reasoning level (that
    is the marking the user asked for — the table carries `gemini-3.7-flash-low/-medium/-high`, not just the base);
    for the others, just the configured model id. [] if the lane has no configured model."""
    base = configured_base(lane)
    if not base:
        return []
    q = quirk(lane)
    if q["style"] == "suffix" and q["levels"]:
        return [f"{base}-{lv}" for lv in q["levels"]]
    return [base]


def use_name_cost(use_name, in_tok, out_tok, lane):
    """API-equivalent $ for a lane use-name at the given token counts — priced at the BASE model's rate for the lane's
    PROVIDER (reasoning changes token COUNT, never the $/token rate). None if the base is unpriced/ambiguous (honest;
    an unpriced lane model reads as $0 est-value, not an invented rate)."""
    from . import pricing
    base, _lv = parse_use_name(use_name, lane)
    prov = lane_provider(lane)
    try:
        return pricing.realtime_cost(base, int(in_tok or 0), int(out_tok or 0), provider=prov)
    except Exception:
        return None


def catalog():
    """The whole table: per lane → {provider, base, reasoning: {style, levels, default}, use_names, price_1m_1m}. This
    is what `spendguard lanes --catalog` prints and what the bandit reads its ARMS from."""
    out = {}
    for lane in lanes():
        base = configured_base(lane)
        uns = use_names(lane)
        # a representative per-1M price (base rate; effort doesn't change it) — None if unpriced/ambiguous
        p1m = use_name_cost(uns[0], 1_000_000, 1_000_000, lane) if uns else None
        out[lane] = {"provider": lane_provider(lane), "base": base, "reasoning": quirk(lane),
                     "use_names": uns, "price_1m_1m": p1m}
    return out


def arms(lanes_filter=None):
    """The bandit's ARM set: (lane, use_name) for every invocable use-name, optionally restricted to `lanes_filter`
    (e.g. advisor.delegate_lanes). Reasoning is a marked axis, so `-low`/`-high` are DISTINCT arms — the bandit can
    learn which effort wins. Skips lanes with no configured/priceable model."""
    keep = set(lanes_filter) if lanes_filter else None
    out = []
    for lane in lanes():
        if keep is not None and lane not in keep:
            continue
        for un in use_names(lane):
            out.append((lane, un))
    return out


def audit_lane_fallback():
    """For EVERY configured lane use-name: the metered-API id it FALLS BACK to when the lane is down (via the one
    equivalence fn adapters.metered_fallback_id), and whether that id is PRICED and — when the model catalog is
    synced — SERVED by the provider. This is the guard for 'a lane's naming must not break its own metered fallback':
    a row with priced=False or served='stale' is a lane that would STRAND a call when its plan is exhausted, instead
    of degrading to the paid API. Read-only, $0 (served_check is cache-first + returns a status, never raises; a
    can't-check is 'unchecked', never a failure). Returns [{lane, provider, use_name, metered_id, reasoning, priced,
    served, ok}] — ok is False only on a CONFIRMED break (unpriced, or a catalog-CONFIRMED stale id)."""
    from . import adapters, pricing
    try:
        from . import vendor_call as _vc
    except ImportError:
        _vc = None                                       # packaging edge — skip the served check, keep the priced one
    out = []
    for lane in lanes():
        prov = lane_provider(lane)
        uns = use_names(lane) or ([configured_base(lane)] if configured_base(lane) else [])
        for un in uns:
            mid, tier = adapters.metered_fallback_id(prov, un)
            try:
                pricing.realtime_cost(mid, 1000, 100)
                priced = True
            except (KeyError, ValueError):               # the UNPRICED/unknown-model signal — narrow, so a refusal
                priced = False                           # or deadline is never downgraded to 'just unpriced'
            served = _vc.served_check(prov, mid) if _vc is not None else None   # returns a status, never raises
            ok = priced and served != "stale"            # only a CONFIRMED-stale id fails; unchecked/None proceed
            out.append({"lane": lane, "provider": prov, "use_name": un, "metered_id": mid,
                        "reasoning": tier, "priced": priced, "served": served, "ok": ok})
    return out


def format_lane_fallback():
    """The `spendguard lanes --fallback` view of audit_lane_fallback: one line per lane use-name → its metered-API
    equivalent, with priced/served and a ✓/✗. Names any BROKEN equivalence (a lane that could not fall back to the
    paid API) at the end, so 'a down lane strands the call' is caught here, not in production."""
    rows = audit_lane_fallback()
    if not rows:
        return "no lanes configured — nothing to audit (set advisor.lane_models)."
    lines = ["lane → metered-API fallback equivalence (when a plan is down/exhausted, the call bills the paid API):",
             "  a lane whose id can't map to a PRICED, SERVED metered id would STRAND the call — those are flagged ✗\n",
             f"  {'lane':<12}{'use-name':<26}{'→ metered id':<24}{'priced':<8}{'served':<12}ok"]
    broken = []
    for r in rows:
        mark = "✓" if r["ok"] else "✗"
        if not r["ok"]:
            broken.append(r)
        served = str(r["served"] if r["served"] is not None else "—")
        lines.append(f"  {r['lane']:<12}{r['use_name']:<26}{r['metered_id']:<24}"
                     f"{('yes' if r['priced'] else 'NO'):<8}{served:<12}{mark}")
    if broken:
        lines.append("\n  ✗ BROKEN — these lanes would NOT fall back to metered (fix the id / pricing / catalog):")
        for r in broken:
            why = "unpriced metered id" if not r["priced"] else f"metered id not served ({r['served']})"
            lines.append(f"      {r['lane']} ({r['use_name']} → {r['metered_id']}): {why}")
    else:
        lines.append("\n  ✓ every lane maps to a priced, served metered id — a down/exhausted plan degrades to the "
                     "paid API, never strands the call.")
    return "\n".join(lines)


def main(argv=None):
    """`spendguard lanes --catalog` — print the lane model catalog (provider · base · reasoning quirk · use-names ·
    price). The source of truth the pricing, recording, and bandit all read."""
    cat = catalog()
    print("Lane model catalog — what each subscription lane can invoke (provider · reasoning quirk · use-names):\n")
    print(f"  {'lane':<13}{'provider':<10}{'reasoning':<18}{'base':<19}{'$/1M+1M':>8}   use-names")
    for lane, c in cat.items():
        q = c["reasoning"]
        lv = f"({q['levels'][0]}..{q['levels'][-1]})" if q["levels"] else ""
        rz = f"{q['style']}{lv}"
        pr = f"${c['price_1m_1m']:.2f}" if c["price_1m_1m"] is not None else "—"
        print(f"  {lane:<13}{(c['provider'] or '?'):<10}{rz:<18}{(c['base'] or '(unset)'):<19}{pr:>8}   "
              f"{', '.join(c['use_names']) or '(no model configured)'}")
    print("\n  reasoning quirk: SUFFIX rides the use-name (gemini …-low/-high); PARAM/THINKING pass effort out-of-band"
          " (use-name is the base). Prices are the BASE rate for the lane's provider — effort changes token count,"
          " not $/token.")
    return 0


if __name__ == "__main__":      # python -m spendguard.lane_catalog
    raise SystemExit(main())

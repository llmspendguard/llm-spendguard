"""Canonical LANE↔METERED reasoning-equivalence map — the ONE persisted source of truth for how a pinned
(provider, model, reasoning) request is realized on its subscription LANE and on that SAME provider's metered API,
so the atomic (lane, metered) pair is a faithful, cost-efficient unit: the $0 lane serves it when up, and a lane
MISS falls back to the SAME provider's paid API at EQUAL-OR-GREATER reasoning — never a different provider, never
LESS reasoning.

WHY THIS EXISTS (the gap Ash named). The equivalence was real but SCATTERED — models.normalize_reasoning (the
metered per-model floor), codex_exec._codex_effort (the codex lane scale), adapters._split/_compose_gemini_reasoning
(the gemini suffix↔param bijection), lane_catalog.REASONING_QUIRK (each lane's protocol) and
adapters.metered_fallback_id (the lane→metered id map). No single place tied them together, so the question "is the
lane→metered fallback faithful?" could neither be READ nor PROVEN. This module unifies them into one derived +
persisted map with a STATUS and PROVENANCE per cell, and a resolver the fallback and the pinned matrix both call.

LANE NAMING — read this once so the map is unambiguous. Each subscription LANE is one PROVIDER's flat-fee plan, and
its "metered twin" is that SAME provider's pay-per-token API. The lane name is NOT the provider name, so it is spelled
out here (source of truth: adapters._LANES {provider: (lane, executor)}):
  · lane "codex"       = the ChatGPT/Codex subscription for OPENAI models   (executor codex_exec)      ↔ OpenAI API
  · lane "claude-code" = the Claude (Max) subscription for ANTHROPIC models (executor subscription_exec)↔ Anthropic API
  · lane "gemini"      = AGY (Antigravity) — the GEMINI SUBSCRIPTION        (executor antigravity_exec) ↔ Gemini API
  · lane "zai-coding"  = the z.ai coding plan for the GLM models            (executor zai_exec)         ↔ z.ai API
So "the gemini lane" and "agy" are the SAME thing — the Gemini subscription — and its fallback is the Gemini metered
API, never another vendor. (agy/Gemini is the one lane where reasoning rides the model-id SUFFIX, e.g.
gemini-3.8-flash-low, while its metered twin takes a reasoning PARAMETER — the two spellings are equivalent, split/
composed by adapters._split_gemini_reasoning / _compose_gemini_reasoning.)

PIN SEMANTICS (Ash's definition). A pin is a PROVIDER + a reasoning FLOOR.
  · PROVIDER-LOCKED: a pinned AGY/Gemini call falls back to the GEMINI metered API, NEVER codex/OpenAI — crossing
    providers is a pin violation. metered_fallback_id never crosses providers (it only re-spells the id within the
    same vendor); resolve_metered() asserts provider == lane's provider, and the lane bandit's cross-provider substitution is
    separately suppressed by no_substitution on the pinned path. Both guards together make the pin provider-tight.
  · EQUAL-OR-GREATER reasoning, resolved in this ORDER:
      1. EQUAL        — the metered API expresses the SAME reasoning the lane applied (the common case).
      2. PROVEN-LESS  — a LESSER metered reasoning an LLM JUDGE decided is equally good over a bake-off sample (the
                        cheaper choice). "equally good" is a MEANING judgement (CLAUDE.md), so it is recorded ONLY
                        with an affirmative agentic verdict — record_equivalence refuses free-text — then persists
                        and overlays the derived map.
      3. ROUND-UP     — no equal available → the next GREATER reasoning the metered model accepts (never silently
                        under-reason on a fallback).

DERIVED, not hardcoded: providers from adapters._LANES, models from advisor.lane_models, the lane scale from
lane_catalog.REASONING_QUIRK / the execs, the metered floor from models.normalize_reasoning, the id map from
adapters.metered_fallback_id. The ONLY authored constant is the canonical reasoning ORDINAL (_REASONING_ORDER) —
the vendors' documented scale order, needed to compare "equal or greater". Bake-off learnings are stored and
OVERLAID, so learning is maintained across re-derivations.
"""
from . import adapters, config, lane_catalog, models

# The requested STANDARD levels a caller pins with. minimal|low|medium|high is the one knob callers use; each
# channel realizes it on its own scale (below). Not user config — the caller-facing ordinal.
STANDARD_LEVELS = ("minimal", "low", "medium", "high")

# The CANONICAL reasoning ordinal, least→most, across the union of the vendors' documented scales (OpenAI API:
# minimal|low|medium|high [+none for gpt-5.5/5.6]; Codex: none|low|medium|high|xhigh|max; Gemini: none|low|medium|
# high). This is the ONE authored fact here — a fixed protocol order, like an alphabet, used only to decide "is the
# metered effort EQUAL to or GREATER than the lane's". `None` (the provider exposes NO reasoning_effort param —
# Anthropic one-shot, plain GLM) is handled separately: it is "does not reason via a param", not a point on this axis.
_REASONING_ORDER = ("none", "minimal", "low", "medium", "high", "xhigh", "max")


def _rank(effort):
    """Position of a reasoning value on the canonical ordinal, or None when it is not a param value (effort is None
    → the provider applies no reasoning_effort param; an unknown string → None, treated as incomparable)."""
    if effort is None:
        return None
    try:
        return _REASONING_ORDER.index(str(effort).strip().lower())
    except ValueError:
        return None


def _lane_models(lane):
    """Every model this lane can be pinned to, from advisor.lane_models — a per-tier map's values, or a single id.
    Suffix-stripped to the BASE (the reasoning level is a separate axis here). De-duped, order-stable."""
    m = (config._cfg_get("advisor", "lane_models", {}) or {}).get(lane)
    raw = []
    if isinstance(m, dict):
        raw = [v for v in m.values() if v]
    elif isinstance(m, str) and m:
        raw = [m]
    seen, out = set(), []
    for r in raw:
        base = lane_catalog.parse_use_name(r, lane)[0]
        if base and base not in seen:
            seen.add(base)
            out.append(base)
    return out


def _lane_effort(lane, model, level):
    """(effort_value, mechanism) the LANE actually applies for a requested standard `level`. mechanism ∈
    {'param','suffix','thinking','none'}; effort_value is the concrete value the lane uses, or None when the lane
    applies no reasoning-effort. Grounded in each exec's real behavior via lane_catalog.REASONING_QUIRK."""
    q = lane_catalog.quirk(lane)
    style = q["style"]
    if style == "param":                              # codex: its OWN scale via _codex_effort (minimal→none, rest pass)
        from . import codex_exec
        return codex_exec._codex_effort(level), "param"
    if style == "suffix":                             # agy/Gemini SUBSCRIPTION: effort rides the id SUFFIX; quirk lists the tiers
        levels = q["levels"]                          # e.g. (low, medium, high) — there is NO 'minimal'/'none' agy spelling
        if level in levels:
            return level, "suffix"
        # a requested level with NO agy suffix spelling (e.g. 'minimal') → the agy lane runs its DEFAULT tier. This
        # MATCHES adapters._compose_gemini_reasoning, which APPENDS this default tier suffix to a bare base id
        # (REASONING_QUIRK['gemini']['default'] = 'medium'): agy serves ONLY the tier-suffixed forms and REJECTS a
        # bare id, so the composer cannot rely on 'return unchanged → lane runs its default'; it must spell the
        # default. It does NOT silently floor to 'low' — to get 'low' on agy you must request 'low' explicitly.
        return (q.get("default"), "suffix")
    if style == "thinking":                           # claude-code: the Claude CLI has no one-shot effort flag
        return None, "thinking"
    return None, "none"                               # zai: no reasoning-effort LEVEL on this lane (thinking budget is
    #                                                   a separate token-budget enrichment, out of this effort map)


def _store_path():
    return config.HOME / "reasoning_equivalence.json"


def load_learnings():
    """The persisted bake-off LEARNINGS only (not the derived cells): {cell_key: {metered_effort, evidence, ts,
    by, sample_n}}. Absent/corrupt → {} (no learnings → the derived equal-or-greater map stands, still reliable)."""
    import json
    try:
        d = json.loads(_store_path().read_text())
    except Exception:
        return {}
    return d.get("learnings") or {}


def _cell_key(lane, model, level):
    return f"{lane}|{model}|{level}"


def record_equivalence(lane, model, level, metered_effort, verdict):
    """Persist a BAKE-OFF-PROVEN equivalence — a LESSER (cheaper) metered reasoning an AGENTIC judge decided is
    EQUALLY GOOD for (lane, model, level). Overlaid by resolve_metered as status='proven_lesser', so the learning is
    maintained and a re-derivation never loses it.

    THIS FUNCTION DOES NOT DECIDE EQUIVALENCE. "equally good" is a MEANING judgement (CLAUDE.md: an LLM decides,
    never a keyword / threshold / the mere presence of a field), so it REQUIRES `verdict` to be an AFFIRMATIVE
    agentic decision from a bake-off + LLM quality judge, and REFUSES anything else. `verdict` must carry:
      judged_equal=True · judge_model (the model that judged) · sample_n>0 (tasks compared) — and ideally
      good_rate_lesser / good_rate_greater / note. A free-text 'evidence' string, or judged_equal not True, is NOT
      proof and is rejected — so the overlay that trusts a stored proven_lesser can only ever read a genuine
      agentic judgement, never an assertion."""
    if not (isinstance(verdict, dict) and verdict.get("judged_equal") is True
            and verdict.get("judge_model") and int(verdict.get("sample_n") or 0) > 0):
        raise ValueError(
            "record_equivalence needs an AGENTIC verdict proving equal quality — {judged_equal: True, judge_model, "
            "sample_n>0, good_rate_lesser, good_rate_greater, note} from a bake-off + an LLM judge. 'equally good' is "
            "a MEANING judgement (CLAUDE.md), so free-text evidence or an unjudged record is refused.")
    import datetime as _dt

    def _store_learning(d):
        d.setdefault("version", 1)
        d.setdefault("learnings", {})[_cell_key(lane, model, level)] = {
            "metered_effort": metered_effort, "verdict": dict(verdict), "by": verdict.get("judge_model"),
            "ts": _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")}
    config.update_json(_store_path(), _store_learning, reason="reasoning-equivalence-bakeoff")
    return load_learnings().get(_cell_key(lane, model, level))


def _lane_use_name(lane, model, lane_eff, mechanism):
    """The ACTUAL invocable id on the LANE for this (model, effort). For the agy/Gemini SUBSCRIPTION lane the effort
    rides the id as a SUFFIX (gemini-3.8-flash + 'low' → 'gemini-3.8-flash-low'); every other lane passes effort
    out-of-band (codex `-c model_reasoning_effort`, claude thinking, zai none), so the use-name is just the base
    model. This is what the ledger records and what the lane CLI is actually called with."""
    if mechanism == "suffix" and lane_eff:
        return f"{model}-{lane_eff}"
    return model


def _metered_priced_served(provider, metered_id):
    """(priced, served) — the TWO distinct availability facts for the SAME-provider metered model, PROVING the atomic
    pair can actually fall back to the EQUAL model, not merely name it (the 'equal model' half of Ash's requirement).
      priced (bool)   — the id has a real $/token card in pricing.py.
      served (str|None)— the provider's live /models catalog status: 'served' | 'stale' (confirmed absent) |
                         'unchecked' (catalog not synced) | None (check unavailable).
    Returns both because they fail INDEPENDENTLY (a priced-but-stale id, or a served-but-unpriced one) and the caller
    needs to tell them apart. Read-only, $0: pricing is a local table and served_check is cache-first and returns a
    STATUS, never raises. Same check as lane_catalog.audit_lane_fallback, but applied to EVERY (model, level) cell —
    that audit only covered each lane's single default use-name, so the cheap-tier models (gpt-5.6-luna,
    claude-haiku-4-5) and every non-default level were never verified."""
    from . import pricing
    try:
        pricing.price(metered_id, provider)            # $0 CARD lookup (NO token counts) — a priced-CHECK, not a cost
        priced = True                                  #   QUOTE, so it feeds no literal token counts to a cost call
    except (KeyError, ValueError):                      # the UNPRICED signal — narrow, so a refusal/deadline is never
        priced = False                                 # downgraded to 'just unpriced'
    served = None
    try:
        from . import vendor_call as _vc
        served = _vc.served_check(provider, metered_id)   # cache-first status; never raises
    except Exception:
        served = None                                  # packaging edge / no catalog — 'can't check', never a failure
    return priced, served


def resolve_metered(lane, model, level):
    """The ONE resolver the lane→metered fallback and the pinned matrix call: for a pinned (lane, model, level), the
    SAME-provider metered (model, reasoning_effort) to fall back to — the EQUAL model at EQUAL-OR-GREATER reasoning —
    plus whether that metered call is actually MAKEABLE (priced + served) and a status + provenance.

    Returns a complete, usable cell:
      {lane, provider, model, level,
       lane_use_name,           # the id the LANE is actually invoked with (agy carries the effort as a suffix)
       lane_effort, lane_mechanism,
       metered_model,           # the EQUAL model on the metered API (same provider, id re-spelled only for agy)
       metered_effort,          # EQUAL, or a bake-off-proven LESSER, or ROUNDED-UP to greater — never under-reason
       metered_priced, metered_served,   # the two raw availability facts for the equal model on the metered API
       availability,            # 'yes' (confirmed served) | 'unverified' (priced, catalog unchecked) | 'no' (stale/unpriced)
       status, provider_locked, provenance}
    status ∈ {equal, proven_lesser, metered_greater, round_up, needs_bakeoff}. PROVIDER-LOCKED by construction —
    metered_fallback_id only re-spells the id WITHIN the same vendor; asserted below so a future change cannot
    silently introduce a cross-provider (e.g. gemini→codex) fallback. metered_greater/round_up are FAITHFUL outcomes
    (Ash: the metered fallback may reason MORE if needed), never defects."""
    prov = lane_catalog.lane_provider(lane)
    if prov is None:
        raise ValueError(f"reasoning_equivalence.resolve: {lane!r} is not a registered lane (adapters._LANES)")
    lane_eff, mech = _lane_effort(lane, model, level)
    metered_id, _suffix_tier = adapters.metered_fallback_id(prov, model)   # SAME provider — only the id spelling changes
    metered_norm = models.normalize_reasoning(metered_id, level)           # what the metered API would send by default

    # ── REASONING resolution: EQUAL → bake-off-proven LESSER → ROUND-UP to greater (never under-reason) ──────────
    learned = load_learnings().get(_cell_key(lane, model, level))
    _verdict = (learned or {}).get("verdict") or {}
    if learned and learned.get("metered_effort") is not None and _verdict.get("judged_equal") is True:
        # A cheaper LESSER effort is trusted ONLY on an AFFIRMATIVE AGENTIC verdict — an LLM judge decided the lesser
        # effort is equally good over a bake-off sample (CLAUDE.md: "equally good" is a MEANING judgement, never a
        # keyword/threshold, never the mere presence of a stored field). record_equivalence REFUSES anything that is
        # not such a verdict, so this overlay can trust it; without one, the cell falls through to the derived rule.
        eff, status = learned["metered_effort"], "proven_lesser"
        provenance = (f"bake-off + {_verdict.get('judge_model', 'judge')} judged metered={eff!r} equally good "
                      f"(n={_verdict.get('sample_n')}; {str(_verdict.get('note', ''))[:90]})")
    else:
        lr, mr = _rank(lane_eff), _rank(metered_norm)
        if lane_eff == metered_norm:                   # identical (incl. both None = neither reasons) → EXACTLY faithful
            eff, status = metered_norm, "equal"
        elif lane_eff is None or metered_norm is None:  # one side reasons via a param, the other has NO param — the two
            eff, status = (metered_norm if metered_norm is not None else lane_eff), "needs_bakeoff"   # scales can't be
            #                                            compared on the ordinal; a bake-off must confirm equivalence
        elif mr is None or lr is None:                 # an unrecognised value on the ordinal → cannot prove ≥ → verify
            eff, status = metered_norm, "needs_bakeoff"
        elif mr >= lr:                                 # the metered default already reasons ≥ the lane → SAFE (greater is OK)
            eff, status = metered_norm, ("equal" if mr == lr else "metered_greater")
        else:                                          # metered default would UNDER-reason → ROUND UP to the lane's tier
            eff, status = lane_eff, "round_up"
        provenance = f"lane {mech}={lane_eff!r}; metered normalize={metered_norm!r}; policy=equal-or-greater"

    # ── EQUAL-MODEL availability: is that metered call actually makeable? (the atomic pair can't fall back otherwise) ─
    # HONEST tri-state — never overclaim availability that was not CONFIRMED:
    #   'no'         — not priced, OR the catalog CONFIRMED the id absent ('stale') → the fallback would STRAND.
    #   'yes'        — priced AND the catalog CONFIRMED it served → the equal-model fallback definitely works.
    #   'unverified' — priced but the catalog is unsynced/unreachable ('unchecked'/None): PROCEED (a can't-check is
    #                  never a rejection, same rule as served_check) but availability is NOT asserted — the runtime
    #                  metered call + served-substitution still confirm at dispatch. So a caller can tell "known-good"
    #                  from "not-yet-verified" instead of a bool that silently calls an unchecked id usable.
    priced, served = _metered_priced_served(prov, metered_id)
    if not priced or served == "stale":
        availability = "no"
    elif served == "served":
        availability = "yes"
    else:
        availability = "unverified"
    return {"lane": lane, "provider": prov, "model": model, "level": level,
            "lane_use_name": _lane_use_name(lane, model, lane_eff, mech),
            "lane_effort": lane_eff, "lane_mechanism": mech,
            "metered_model": metered_id, "metered_effort": eff,
            "metered_priced": priced, "metered_served": served, "availability": availability,
            "status": status, "provider_locked": True, "provenance": provenance}


def resolve_lane(provider, metered_model, reasoning=None):
    """The REVERSE direction of the map (metered → lane) — so the mapping is BIDIRECTIONAL. Given a SAME-provider
    METERED (model, reasoning) — e.g. a name a USER requested in metered form — return the subscription LANE that
    serves it and the exact lane USE-NAME (how the lane is CALLED for that reasoning), or None if the provider has
    NO lane (a metered-only vendor). This is the inverse of adapters.metered_fallback_id (lane use-name → metered
    id + reasoning): the lane and the metered API can NAME the same model differently and pass reasoning
    differently, so both directions are needed to route a pin regardless of which form the caller holds.

    agy/Gemini is the case where the names DIFFER: the effort rides the id SUFFIX on the lane
    (gemini-3.8-flash + 'low' → gemini-3.8-flash-low) while the metered API takes the bare id + a reasoning param —
    so this composes the suffix (adapters._compose_gemini_reasoning). Every other lane passes reasoning out-of-band
    (codex param / claude thinking / zai none), so the lane use-name IS the metered id. Round-trips with
    metered_fallback_id: split(compose(x)) == x and compose(split(x)) == x for a valid tier."""
    lane = adapters._LANES.get(provider, (None,))[0]
    if not lane:
        return None                                      # metered-only vendor — no subscription-lane form exists
    q = lane_catalog.quirk(lane)
    if q["style"] == "suffix" and reasoning in q["levels"]:
        use_name = adapters._compose_gemini_reasoning(metered_model, reasoning)   # bare id + tier → the suffixed lane id
    else:
        use_name = metered_model                         # param/thinking/none lanes: the use-name IS the metered id
    return {"lane": lane, "provider": provider, "lane_use_name": use_name, "reasoning": reasoning}


def derive_map():
    """The whole clear map: {lane: {provider, models: {model: {level: <resolve cell>}}}}, derived from the current
    config + execs and overlaid with any persisted bake-off learnings. This is the JSON Ash asked for — inspectable,
    provable, and it MAINTAINS THE LEARNING (learnings overlay via resolve). $0, no LLM, no network."""
    out = {}
    for lane in lane_catalog.lanes():
        prov = lane_catalog.lane_provider(lane)
        mdls = _lane_models(lane)
        cells = {}
        for model in mdls:
            cells[model] = {lv: resolve_metered(lane, model, lv) for lv in STANDARD_LEVELS}
        out[lane] = {"provider": prov, "models": cells}
    return out


def full_map():
    """derive_map() wrapped with metadata + the policy statement — the exact JSON persisted / shown."""
    import datetime as _dt
    return {"version": 1,
            "policy": "pinned = same PROVIDER + reasoning FLOOR; fallback lane→metered stays same-provider at "
                      "EQUAL, else BAKE-OFF-PROVEN-lesser, else ROUND-UP to greater (never under-reason, never "
                      "cross provider)",
            "standard_levels": list(STANDARD_LEVELS), "reasoning_order": list(_REASONING_ORDER),
            "generated_ts": _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds"),
            "learnings": load_learnings(), "map": derive_map()}


def save_map(path=None):
    """Persist the full map for inspection (the derived view + the persisted learnings) THROUGH config.update_json —
    atomic, backed up, and refuses an unparseable target — never a bare write_text (the every-json-write-backs-up
    rule). Learnings are the durable half and are preserved verbatim; the derived half is re-derivable, so a rewrite
    never loses a bake-off learning."""
    p = path or _store_path()
    doc = full_map()                                   # includes the current learnings (load_learnings)

    def _write_full_map(d):
        learnings = d.get("learnings") or doc.get("learnings") or {}   # keep learnings already on disk
        d.clear()
        d.update(doc)
        d["learnings"] = learnings                     # learnings verbatim — never clobbered by the re-derive
    config.update_json(p, _write_full_map, reason="reasoning-equivalence-map")
    return str(p)


def bakeoff_candidates():
    """Cells a bake-off would REFINE (status ∈ {metered_greater, needs_bakeoff}) — where the metered fallback
    reasons MORE than the lane (a proven-equal lesser would be cheaper) or the pair can't be compared on the
    ordinal (asymmetric effort). Each carries the (lane, model, level, lane_effort, metered_effort) a bake-off
    would test. round_up/equal/proven_lesser cells are already reliable and are NOT listed. $0, no LLM."""
    out = []
    for lane, rec in derive_map().items():
        for model, levels in rec["models"].items():
            for lv, cell in levels.items():
                if cell["status"] in ("metered_greater", "needs_bakeoff"):
                    out.append({"lane": lane, "model": model, "level": lv, "provider": cell["provider"],
                                "lane_effort": cell["lane_effort"], "metered_effort": cell["metered_effort"],
                                "status": cell["status"]})
    return out


def audit_cells():
    """A flat, sortable audit of every cell: [{lane, provider, model, level, lane_use_name, lane_effort,
    metered_model, metered_effort, metered_priced, metered_served, availability, status}] — the rows behind
    format_map(). Read-only, $0."""
    rows = []
    for lane, rec in derive_map().items():
        for model, levels in rec["models"].items():
            for lv, c in levels.items():
                rows.append({k: c.get(k) for k in ("lane", "provider", "model", "level", "lane_use_name",
                                                   "lane_effort", "metered_model", "metered_effort",
                                                   "metered_priced", "metered_served", "availability", "status")})
    return rows


def format_map():
    """`spendguard lanes --reasoning-map`: one line per (lane, model, level) → the SAME-provider metered EQUAL-model
    call it falls back to (id + effort), its status and whether it is usable (priced+served), grouped by lane, with
    the policy and any NON-equal or UNUSABLE cells called out at the end."""
    rows = audit_cells()
    if not rows:
        return "no lanes configured — nothing to map (set advisor.lane_models)."
    lines = ["lane→metered EQUIVALENCE — for each lane's model+reasoning, the SAME-provider metered call (EQUAL model,",
             "EQUAL-or-GREATER reasoning) the atomic pair falls back to when the $0 lane is down/exhausted:",
             f"  {'lane':<12}{'lane use-name':<24}{'lvl':<8}{'lane→':<7}{'metered model':<28}{'eff':<8}{'ok':<4}status"]
    for r in sorted(rows, key=lambda r: (r["lane"], r["model"], STANDARD_LEVELS.index(r["level"])
                                         if r["level"] in STANDARD_LEVELS else 99)):
        le = "none*" if r["lane_effort"] is None else r["lane_effort"]
        me = "none*" if r["metered_effort"] is None else r["metered_effort"]
        ok = {"yes": "✓", "unverified": "?", "no": "✗"}.get(r["availability"], "?")
        lines.append(f"  {r['lane']:<12}{r['lane_use_name']:<24}{r['level']:<8}{le:<7}"
                     f"{r['metered_model']:<28}{me:<8}{ok:<4}{r['status']}")
    nonequal = [r for r in rows if r["status"] != "equal"]
    stranded = [r for r in rows if r["availability"] == "no"]
    unverified = [r for r in rows if r["availability"] == "unverified"]
    lines.append("\n  * none = the provider exposes NO reasoning_effort param (Anthropic one-shot / plain GLM) —"
                 " both channels agree, so it is EQUAL by construction.")
    lines.append("  availability (✓ served / ? unverified / ✗ stranded): can the EQUAL-model metered call be made? "
                 "✗ (unpriced or catalog-confirmed absent) would STRAND the pin; ? = priced but the catalog is "
                 "unsynced, so proceed and let dispatch confirm. Surfaced here, never discovered in production.")
    if stranded:
        lines.append("  ✗ STRANDED — the equal model is not priced/served on the metered API (fix pricing/catalog, "
                     "or map to an equal-or-greater MODEL):")
        for r in stranded:
            lines.append(f"      {r['lane']}/{r['metered_model']} @ {r['level']}: "
                         f"priced={r['metered_priced']} served={r['metered_served']}")
    if unverified:
        lines.append(f"  ? UNVERIFIED ({len(unverified)} cell(s)) — priced but the served-list cache is unsynced; "
                     "run `spendguard sync-catalog` to confirm, then re-check.")
    if nonequal:
        lines.append("  reasoning cells that are not identical (all SAFE — never under-reason; 'metered_greater'/"
                     "'round_up' honor equal-or-greater; 'needs_bakeoff' = verify the pair):")
        for r in nonequal:
            lines.append(f"      {r['lane']}/{r['lane_use_name']} @ {r['level']}: lane={r['lane_effort']} → "
                         f"metered={r['metered_effort']} ({r['status']})")
    else:
        lines.append("  every cell is EQUAL reasoning — the lane and its metered fallback apply identical effort.")
    return "\n".join(lines)

"""model_preflight — the FIXED, TESTED MAP that makes a model id CALLABLE-or-not answerable up front, before a batch
spends a cent on it. The recurring failure it kills: a stale/renamed id (gemini-3-flash → the served
gemini-3-flash-preview) or an unpriced id discovered MID-RUN, after money is spent and every call 404s.

It resolves a whole candidate list against the LIVE served-catalog + pricing FIRST, names the correction for a stale
id AGENTICALLY (vendor_call.closest_served → pricing._same_model_as_ours, an identity MEANING call, never string
distance), and reports usable/not so the caller refuses or swaps the id before dispatch. $0 for a served id (a cached
catalog read); a STALE id pays for exactly one tiny identity judgement — and closest_served RECORDS that judgement
(fact_key 'served_id'), so a re-run reads the cache instead of re-paying. That, plus per-spec isolation below, is why
a crash mid-list loses nothing expensive.

EXPLICIT STATES, never binary: served ∈ {served, stale, unchecked, unknown-provider, empty, error} — a can't-check
('unchecked', catalog not synced) is NEVER conflated with a confirmed fail ('stale'); each state drives its own
action (stale → swap the named correction; unchecked → sync-catalog to resolve; unpriced → add pricing).

Composes existing primitives — adapters.provider_for, adapters.metered_fallback_id (the lane→metered id map),
vendor_call.served_check / closest_served, pricing.realtime_cost — it re-implements none of them; it makes them a
single pre-spend gate. Run it before any batch, and on a cadence (`spendguard verify` calls it)."""

from . import adapters, config, gate, pricing, vendor_call


def _split_spec(spec):
    """'vendor:model' → (vendor, model); bare 'model' → (provider_for(model) or None, model). Parsing only."""
    spec = str(spec).strip()
    if ":" in spec:
        prov, model = spec.split(":", 1)
        return prov.strip() or None, model.strip()
    try:
        return adapters.provider_for(spec), spec
    except Exception:
        return None, spec


def _closest_or_halt(prov, metered_id):
    """The currently-served SAME-model id for a stale one (agentic), or None if it can't be determined. A DELIBERATE
    stop — a spend/budget refusal or a dispatch deadline from the tiny identity call — PROPAGATES (via the one
    gate.deliberate_stop_types concept), never downgraded to 'no correction': being over budget must halt the
    preflight, not proceed as if the id were merely uncorrectable. The result is fact-cached by closest_served, so a
    re-run never re-pays for it."""
    try:
        same, _live = vendor_call.closest_served(prov, metered_id)
        return same
    except Exception as e:
        if isinstance(e, gate.deliberate_stop_types()):
            raise                                         # deliberate stop → propagate, do not swallow
        return None                                       # a transient/lookup failure → correction unknown, proceed


def _preflight_one(spec, correct):
    """Resolve ONE non-empty spec → its result dict (see preflight_models). A deliberate stop from _closest_or_halt
    propagates through untouched (the caller isolates any other failure into an error row)."""
    prov, model = _split_spec(spec)
    if not prov:
        return {"spec": spec, "provider": None, "model": model, "metered_id": model, "served": "unknown-provider",
                "corrected": None, "priced": False, "usable": False,
                "note": "no provider for this id — pass it as vendor:model"}
    metered_id, _tier = adapters.metered_fallback_id(prov, model)       # what the metered API is really called with
    served = vendor_call.served_check(prov, metered_id)                 # 'served' | 'stale' | 'unchecked'
    corrected = _closest_or_halt(prov, metered_id) if (served == "stale" and correct) else None
    check_id = corrected or metered_id
    try:
        pricing.realtime_cost(check_id, 1000, 100)
        priced = True
    except (KeyError, ValueError):                                      # the UNPRICED/unknown-model signal, narrowly
        priced = False
    # usable = callable EXACTLY AS WRITTEN: served (or unchecked — a can't-know is not a no) AND priced. A STALE id is
    # NOT usable even when a correction exists, because dispatch REFUSES a stale id (it does not auto-swap) — the
    # `corrected` field is the fix the CALLER must apply (edit config, or swap the id for this batch), not an
    # auto-substitution. So a stale id fails preflight/verify until it is actually changed.
    usable = priced and served in ("served", "unchecked")
    if served == "stale" and corrected:
        note = f"STALE id — use '{corrected}' (currently-served same model)"
    elif served == "stale":
        note = "STALE id — no served equivalent found; fix the id before spending"
    elif not priced:
        note = f"UNPRICED ({check_id}) — add it to pricing.py (≥2 sources) before spending"
    elif served == "unchecked":
        note = "unchecked (catalog not synced) — proceeds; `spendguard sync-catalog` to validate the id is live"
    else:
        note = "ok — served + priced"
    return {"spec": spec, "provider": prov, "model": model, "metered_id": metered_id, "served": served,
            "corrected": corrected, "priced": priced, "usable": usable, "note": note}


def preflight_models(specs, correct=True):
    """Resolve `specs` (each 'vendor:model' or bare 'model') against the live catalog + pricing. $0 per served id;
    when correct=True a STALE id costs one tiny (fact-cached) agentic identity call to name its current replacement.
    Returns ONE dict per input spec — {spec, provider, model, metered_id, served, corrected, priced, usable, note} —
    where served ∈ {served, stale, unchecked, unknown-provider, empty, error}; usable is True only when a
    served-or-unchecked, PRICED id (the requested one or its correction) exists. Nothing generated; nothing spent on a
    model call. EVERY spec produces a row (empties + errors included), so len(out) == len(specs); a spec that errors
    is a LOUD error row, never a lost list or a wedge; only a DELIBERATE stop (gate/budget/dispatch) propagates."""
    out = []
    for spec in specs:
        if not str(spec).strip():
            out.append({"spec": "", "provider": None, "model": "", "metered_id": "", "served": "empty",
                        "corrected": None, "priced": False, "usable": False,
                        "note": "empty/whitespace spec in the list — not a callable id"})   # surfaced, not skipped
            continue
        try:
            out.append(_preflight_one(str(spec).strip(), correct))
        except Exception as e:
            if isinstance(e, gate.deliberate_stop_types()):
                raise                                     # over budget / refused / deadline → halt, don't paper over it
            out.append({"spec": str(spec), "provider": None, "model": str(spec), "metered_id": str(spec),
                        "served": "error", "corrected": None, "priced": False, "usable": False,
                        "note": f"preflight error (this spec only): {str(e)[:80]}"})
    return out


def configured_specs():
    """Every model id THIS install is configured to call — advisor.model, advisor.judge_model, each
    advisor.lane_models value, and each model in advisor.tiers. The default target of `spendguard preflight`: 'are all
    my configured ids still callable?', answerable $0 on a cadence."""
    seen, specs = set(), []
    def _add(m):
        if m and m not in seen:
            seen.add(m)
            specs.append(m)
    _add(config._cfg_get("advisor", "model", None))
    _add(config._cfg_get("advisor", "judge_model", None))
    for _lane, m in (config._cfg_get("advisor", "lane_models", {}) or {}).items():
        _add(m)
    tiers = config._cfg_get("advisor", "tiers", None) or {}
    if isinstance(tiers, dict):
        for _g, models in tiers.items():
            for m in (models or []):
                _add(m)
    return specs


def format_preflight(rows):
    """Human view of preflight_models rows: one line per spec → its metered id, served/priced, and a ✓/✗, then any
    STALE/UNPRICED ids named with their fix. So 'which of my model ids would fail a call' is a glance, not a run."""
    if not rows:
        return "no model ids to preflight (none configured; pass ids explicitly)."
    lines = ["model id preflight — resolved against the live served-catalog + pricing, $0 before any spend:",
             f"  {'spec':<34}{'→ metered id':<26}{'served':<11}{'priced':<8}ok"]
    for r in rows:
        mark = "✓" if r["usable"] else "✗"
        lines.append(f"  {(r['spec'] or '(empty)'):<34}{(r['metered_id'] or '—'):<26}{r['served']:<11}"
                     f"{('yes' if r['priced'] else 'NO'):<8}{mark}")
    flagged = [r for r in rows if not r["usable"]]
    if flagged:
        lines.append("\n  ✗ NOT CALLABLE as written — fix before spending:")
        for r in flagged:
            lines.append(f"      {r['spec'] or '(empty)'}: {r['note']}")
    corrections = [r for r in rows if r["corrected"]]
    if corrections:
        lines.append("\n  ↺ STALE ids with a served replacement (swap the id):")
        for r in corrections:
            lines.append(f"      {r['spec']}  →  {r['corrected']}")
    if not flagged and not corrections:
        lines.append("\n  ✓ every id is served (or unchecked) + priced — callable as written.")
    return "\n".join(lines)


def cmd(argv=None):
    """`spendguard preflight [models…]` — resolve model ids before spending. No args → all CONFIGURED ids (the
    cadence check). Exit non-zero if any id is not callable as written (stale-without-fix, unpriced, unknown provider)."""
    import argparse
    ap = argparse.ArgumentParser(prog="spendguard preflight",
                                 description="Resolve model ids against the live catalog + pricing BEFORE spending.")
    ap.add_argument("models", nargs="*", help="model specs (vendor:model or bare); omit to check all CONFIGURED ids")
    ap.add_argument("--no-correct", action="store_true",
                    help="skip the tiny agentic call that names a stale id's current replacement (report stale only)")
    a = ap.parse_args(argv)
    specs = a.models or configured_specs()
    if not specs:
        print("nothing to preflight — no models given and none configured (advisor.model / lane_models / tiers).")
        return 0
    rows = preflight_models(specs, correct=not a.no_correct)
    print(format_preflight(rows))
    return 0 if all(r["usable"] for r in rows) else 1

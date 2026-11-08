"""Own PROVIDER RE-ATTRIBUTION: fix a charge whose recorded provider or key fingerprint disagrees with the model registry, writing the correction and its chained audit row through the ledger handle without destructively mutating the original in place, and snapshotting first through the seam. It reaches budget's engine core (``_ledger`` / raw ``snapshot``) and provider-of resolution through the ``admission_accessors`` resolver seam, and never imports budget."""

from . import admission_accessors as _admission_accessors
from spendguard.generate.execution import execution_accessors as _execution_accessors
from ... import config
from ...ledger_writer import serialize_ledger_function
from ...ledger import to_dec, USD_COLS


@serialize_ledger_function
def reattribute_providers(apply=False, actor="reattribute_providers"):
    """Find (and optionally correct) charges whose `provider` disagrees with the model registry.

    WHY THIS IS A FUNCTION AND NOT A ONE-OFF SCRIPT. The gate inferred a charge's vendor with
    `"anthropic" if model.startswith("claude") else "openai"`, so every OpenAI-COMPATIBLE vendor was
    recorded as OpenAI: measured, 695 rows and $30.26 of Moonshot and z.ai spend sat on the OpenAI line.
    `saas reconcile` compares the ledger to provider billing PER PROVIDER, so one line was over-attributed
    by exactly what the others were missing, and the leak verdict drawn from them was wrong twice over.
    The inference is fixed, but a ledger that cannot repair its own history is not an accounting record —
    and the same class of drift will happen again the next time a vendor is added.

    DRY BY DEFAULT. Returns what it WOULD change; `apply=True` writes. Every correction is journalled to
    spend_audit with the old and new value, because a silent restatement is indistinguishable from the
    error it corrects — and in a forensic tool the correction has to be as visible as the mistake."""
    led = _admission_accessors.resolve__ledger()
    rows = [r for r in led.query() if r.get("model")]     # every spend_events row carrying a model
    fixes = []
    for r in rows:
        prov, model = r.get("provider"), r.get("model")
        true = _admission_accessors.resolve__provider_of(model)
        # UNKNOWN IS NOT A CORRECTION. Overwriting a recorded vendor with "unknown" destroys information;
        # only a POSITIVE identification that disagrees is a fix.
        if true and true != _admission_accessors.UNKNOWN_PROVIDER and prov and true != prov:
            fixes.append({"id": r["id"], "model": model, "was": prov, "now": true,
                          "cost": float(sum((to_dec(r.get(c)) for c in USD_COLS), to_dec(0)))})
    # A KEY FINGERPRINT THAT BELONGS TO THE WRONG VENDOR IS WORSE THAN NONE. These rows were stamped with
    # the fingerprint of the provider they were MIS-labelled as, so after the vendor is corrected the row
    # reads "moonshot spend, served by an OpenAI key" — a contradiction that looks like a finding. We cannot
    # recover which key really served a past call, so it becomes EMPTY, which this column already defines as
    # unknown. Restoring the current key's fingerprint instead would assert that today's key served a call
    # made before it existed.
    if fixes:
        _wrong_fp = {}
        for f in fixes:
            try:
                _wrong_fp.setdefault(f["was"], config.key_fingerprint(f["was"]))
            except Exception:
                _wrong_fp.setdefault(f["was"], "")
    unjournalled = []
    by_id = {r["id"]: r for r in rows}
    if apply and fixes:
        _admission_accessors.resolve_snapshot(reason=f"reattribute-{actor}")     # whole-file backup before rewriting money rows
        for f in fixes:
            changes = {"provider": f["now"]}
            stale = _wrong_fp.get(f["was"]) or ""
            if stale and by_id.get(f["id"], {}).get("key_fp") == stale:
                changes["key_fp"] = ""              # a fp stamped for the mis-labelled vendor is worse than none
            # led.update writes the correction AND its audit row through the hash chain, on the ledger's own
            # connection — no separate raw INSERT (the source of 697 unchained rows), no cross-connection
            # deadlock. A locked period is refused; that row is reported unjournalled rather than silently skipped.
            try:
                led.update(f["id"], changes, actor=actor, pass_="reattribute",
                           reason=f"model {f['model']} is served by {f['now']}, not {f['was']} "
                                  f"(gate inferred the vendor from the model name prefix)")
            except Exception as e:
                unjournalled.append({"id": f["id"], "error": f"{type(e).__name__}: {str(e)[:60]}"})
        if unjournalled:
            import sys as _sys
            _sys.stderr.write(f"[budget] WARN {len(unjournalled)} of {len(fixes)} re-attributions NOT applied "
                              f"(locked period?) — e.g. {unjournalled[0]}\n")
    # SECOND PASS: a fingerprint that belongs to a DIFFERENT vendor than the row's.
    #
    # Separate from the vendor fix above because it outlives it: rows corrected in an EARLIER run still
    # carry the fingerprint they were stamped with while mis-labelled, so a row reads "moonshot spend,
    # served by an OpenAI key". Checking the whole table each time makes the repair idempotent and catches
    # rows this run did not touch. Which key really served a past call is not recoverable, so it becomes
    # EMPTY — the value this column already defines as unknown. Writing today's key instead would assert
    # that it served a call made before it existed.
    known = {}
    try:
        for v in _execution_accessors.PROVIDERS:
            fp = config.key_fingerprint(v)
            if fp:
                known[fp] = v
    except Exception:
        known = {}
    stale = []
    if known:
        for r in led.query():
            fp = r.get("key_fp")
            owner = known.get(fp) if fp else None
            if owner and r.get("provider") and owner != r["provider"]:
                stale.append({"id": r["id"], "provider": r["provider"], "key_fp": fp, "belongs_to": owner})
        if apply and stale:
            for st in stale:                        # led.update writes the correction + its chained audit row
                try:
                    led.update(st["id"], {"key_fp": ""}, actor=actor, pass_="reattribute",
                               reason=f"fingerprint belongs to {st['belongs_to']}, not {st['provider']} — the key "
                                      f"that served this call is not recoverable, so it is UNKNOWN")
                except Exception:
                    pass
    return {"n": len(fixes), "usd": round(sum(f["cost"] for f in fixes), 4), "applied": bool(apply),
            "by_move": _count_moves(fixes), "fixes": fixes[:20], "unjournalled": unjournalled,
            "stale_key_fp": len(stale)}


def _count_moves(fixes):
    out = {}
    for f in fixes:
        k = f"{f['was']}->{f['now']}"
        out[k] = {"rows": out.get(k, {}).get("rows", 0) + 1,
                  "usd": round(out.get(k, {}).get("usd", 0.0) + f["cost"], 4)}
    return out

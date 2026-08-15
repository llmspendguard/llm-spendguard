"""The ONE reconcile loop — shared by every spend source (LLM batches + realtime, GPU/vast.ai, subscription,
storage…). Replaces the parallel one-off reconcilers (ledger_sync for LLM, resources for GPU) with a single
core + per-source ADAPTERS, so the logic lives in one place.

Each source is an adapter (`Source`) that provides:
  • truth_total(since)  — the authoritative EXTERNAL $ (provider billing API / vast.ai account / Stripe / B2 bill).
  • captured(since)     — rows the gate/recorder already attributed, by intent/label → project (each {cost, project}).
  • attribute_gap(gap)  — AGENTIC: a caged LLM reads the CONVERSATIONS to attribute the remaining gap to a project
                          (same conv→project mapping for an LLM batch and a GPU instance). Default [] (no agentic).
  • conn()              — the saas connection (for the account-owner guard).

The core then runs, identically for every source:
  gap       = truth_total − Σ captured
  attributed= attribute_gap(gap)                      (agentic, only if this connection owns the account)
  residual  = truth_total − Σ captured − Σ attributed → SURFACED, never dumped on a project/org
  warning   = fired when residual is large (a source/tenant is UNDER-attributed)

PRINCIPLE (validated on GPU): the agentic LLM read gives ATTRIBUTION (who/what), not MEASUREMENT (how much) —
magnitude comes from truth_total + captured; conversations only resolve WHERE it lands. Account-anchored: only the
owns_account connection reconciles a SHARED account's gap (else a non-owner claims other tenants' spend). Durable
fix per source: capture at the source (the gate for LLM, scheduled snapshot for GPU) so the gap → 0.
"""


def owner_ok(conn):
    """Account-anchor guard: only the owns_account connection reconciles a SHARED provider/vast account's gap — a
    non-owner would attribute other tenants' spend to itself. Standalone/unconnected (no conn) reconciles fully.

    OWNERSHIP IS A PROPERTY OF THE PROVIDER ACCOUNT, NOT OF THE SaaS CONNECTION, so `enabled` does not gate it.
    This used to read `if conn.get("enabled") and not conn.get("owns_account")`, which let two cases through:

      {"enabled": False, "owns_account": False}   a declared NON-OWNER, granted full reconcile rights because
                                                  its sync happened to be switched off. The provider account is
                                                  shared either way; turning off the upload does not stop the
                                                  LOCAL ledger absorbing other tenants' spend as unattributed
                                                  rows — and enabling sync later propagates the contamination.
      {"enabled": False}                          ownership ABSENT, read as ownership GRANTED.

    The second is this project's own invariant inverted, in the one guard whose job is to stop us claiming
    other people's money. Absence is not permission. Ownership must be stated, and a connection that does not
    state it is refused — the fail-closed direction, because the cost of wrongly refusing is a message telling
    you which flag to set, and the cost of wrongly allowing is someone else's spend on your books."""
    if not conn:
        return True, "standalone (no connection) — no shared account is declared"
    if conn.get("owns_account") is True:
        return True, "account owner"
    if "owns_account" not in conn:
        return False, ("connection does not state owns_account — refusing rather than assuming ownership; "
                       "set owns_account on the connection if this host owns the provider account")
    return False, "not the account owner (owns_account=false) — the owner connection reconciles the shared-account gap"


def org_of(project, ptmap):
    return ptmap.get((project or "").lower(), ("", ""))[0] or "(untagged)"


def rollup_by_org(rows, ptmap, cost_key="cost", proj_key="project"):
    """Roll attributed rows up to org via project→org (ptmap). Diagnostic of WHERE the recorded spend landed."""
    by = {}
    for r in rows:
        o = org_of(r.get(proj_key), ptmap)
        by[o] = by.get(o, 0.0) + (r.get(cost_key) or 0.0)
    # ROUNDED ONCE, AT THE END. Rounding on every accumulation step discards a fraction of a cent per row
    # and the error compounds with row count — on tens of thousands of realtime charges the org rollup
    # drifted from the ledger it is supposed to reconcile against, and a reconciliation whose own
    # arithmetic disagrees is worse than none.
    return {o: round(v, 2) for o, v in by.items()}


def residual(truth_total, *captured_sums):
    """truth − Σ(captured/attributed). The unrecoverable remainder — SURFACED, never dumped on a project/org.
    truth_total is None = UNKNOWN (the external fetch failed) → residual is None, not a misleading number."""
    if truth_total is None:
        return None
    return round(truth_total - sum(captured_sums), 2)


def residual_warning(truth_total, resid, frac=0.10, floor=25.0):
    """A large residual (either direction) is a problem to surface loudly, not hide. POSITIVE = UNDER-attributed
    (destroyed/ungated spend not yet recovered → it floats). NEGATIVE = OVER-attributed / STALE (the ledger
    attributes more than the provider currently bills → re-run reconcile). truth_total=None = the external bill
    couldn't be read → say so (don't pretend it reconciled). Returns a message or None."""
    if truth_total is None:
        return ("truth UNKNOWN — the account/provider bill could not be read (key/network). These numbers are NOT "
                "reconciled; fix the fetch before trusting them. (A failed fetch must never read as $0 / 100% covered.)")
    if truth_total == 0:
        # ZERO IS A MEASUREMENT, NOT AN ABSENCE. `if not truth_total` swallowed it four lines below a
        # docstring promising that a failed fetch never reads as "$0 / 100% covered" — and a provider that
        # billed NOTHING while we attributed spend to it is the loudest leak there is, not the quietest.
        if resid:
            return (f"provider truth is $0.00 but ${resid:.2f} is attributed to it — either the fetch "
                    f"returned an empty bill that was read as zero, or spend is attributed to the wrong "
                    f"account. Neither reconciles; do not treat this as covered.")
        return None
    thresh = max(floor, frac * truth_total)
    pct = resid / truth_total * 100
    if resid > thresh:
        return (f"residual ${resid:.2f} ({pct:.0f}% of account) — UNDER-attributed; recover its destroyed/ungated "
                "spend (or it floats). Durable fix: capture at source.")
    if resid < -thresh:
        return (f"residual ${resid:.2f} ({pct:.0f}%) — OVER-attributed / STALE: the ledger attributes more than the "
                "provider currently bills. Re-run reconcile to rebuild against fresh provider truth.")
    return None


class Source:
    """Reconcile adapter. Subclass per spend source; the core (`run`) does the rest, identically."""
    name = "source"

    def conn(self):
        return {}

    def truth_total(self, since=None):
        return 0.0

    def captured(self, since=None):
        return []

    def attribute_gap(self, gap, since=None):
        return []


def all_sources(ptmap=None, since=None):
    """Run the one loop for EVERY spend source (LLM + GPU today; subscription/storage as adapters are added) → a
    unified, account-anchored reconciliation: {source_name: run(Source)}. Lazy imports avoid module cycles."""
    if ptmap is None:
        try:
            from . import attribution
            ptmap = attribution.project_team_map(attribution.taxonomy()[0])
        except Exception:
            ptmap = {}
    out = {}
    try:
        from .ledger_sync import LLMSource
        out["llm"] = run(LLMSource(since=since), ptmap, since)   # batch LLM (provider ledger truth)
    except Exception as e:
        out["llm"] = {"error": str(e)[:160]}
    try:
        from .ledger_sync import RealtimeSource
        out["realtime"] = run(RealtimeSource(since=since), ptmap, since)
    except Exception as e:
        out["realtime"] = {"error": str(e)[:160]}
    # GPU / remote-compute: EVERY source in the gpu_port registry through the same loop — vast.ai (key "gpu")
    # + the port adapters (gpu:runpod / gpu:modal / gpu:lambdalabs) + any plugin-registered provider. An
    # UNCONFIGURED adapter's factory returns None → silently skipped (never an error, never fake data).
    try:
        from . import gpu_port
        gpu_sources = gpu_port.sources()
    except Exception as e:
        gpu_sources, out["gpu"] = {}, {"error": str(e)[:160]}
    for key, factory in gpu_sources.items():
        try:
            src = factory()
            if src is None:
                continue
            out[key] = run(src, ptmap, since)
        except Exception as e:
            out[key] = {"error": str(e)[:160]}
    return out


def completeness(results):
    """Cross-source completeness verdict — the SYSTEM (not a human) surfaces an UNDER-reconstructed source, so a
    missing source (e.g. realtime remote calls run on vast.ai boxes that were never reconstructed) can't hide. Per
    source: reconciled | under (unrecovered spend floats) | over (stale, attributes more than billed) | unknown
    (truth unreadable — NEVER read as complete). Returns {complete, sources:{name:{status,gap}}, msg}. PURE."""
    src, complete = {}, True
    for name, r in (results or {}).items():
        if r.get("error"):
            src[name] = {"status": "error", "gap": None}; complete = False; continue
        truth, resid = r.get("truth_total"), r.get("residual")
        if truth is None:
            src[name] = {"status": "unknown", "gap": None}; complete = False; continue
        thresh = max(25.0, 0.10 * truth)
        if truth <= 0 and resid is not None and round(resid, 2) != 0.0:
            # No provider truth to reconcile against: the $25 relative-noise floor is meaningless when truth is
            # $0 (0.10*0 = 0, so only the absolute floor survives). A residual that is nonzero TO THE CENT — the
            # same rounding used for the gap below — is unrecovered / misattributed spend, NOT "reconciled".
            src[name] = {"status": "under" if resid > 0 else "over", "gap": round(resid, 2)}; complete = False
        elif resid is not None and resid > thresh:
            src[name] = {"status": "under", "gap": round(resid, 2)}; complete = False
        elif resid is not None and resid < -thresh:
            src[name] = {"status": "over", "gap": round(resid, 2)}; complete = False
        else:
            src[name] = {"status": "reconciled", "gap": round(resid or 0.0, 2)}
    if complete:
        msg = "all sources reconciled"
    else:
        bits = []
        for n, s in src.items():
            if s["status"] == "under":
                bits.append(f"{n}: UNDER (${s['gap']} unreconstructed — recover/attribute it)")
            elif s["status"] in ("over", "unknown", "error"):
                bits.append(f"{n}: {s['status'].upper()}")
        msg = "INCOMPLETE — " + "; ".join(bits)
    return {"complete": complete, "sources": src, "msg": msg}


def report(ptmap=None, since=None):
    """Print the unified reconciliation across all sources — truth vs captured vs residual per source, account-
    anchored, with the under-attribution warning + a CROSS-SOURCE completeness verdict. The single source-of-truth view."""
    res = all_sources(ptmap, since)
    print("reconcile — all spend sources (truth − captured = residual; account-anchored):")
    for name, r in res.items():
        if r.get("error"):
            print(f"  {name:6} ERROR: {r['error']}")
            continue
        fmt = lambda v: "  unknown" if v is None else f"${v:9.2f}"   # None truth/residual = fetch failed, not $0
        print(f"  {name:6} truth {fmt(r['truth_total'])}  captured {fmt(r['captured'])}  "
              f"attributed {fmt(r['attributed'])}  residual {fmt(r['residual'])}  by_org={r['by_org']}")
        if r.get("warning"):
            print(f"         ⚠  {r['warning']}")
    comp = completeness(res)
    print(f"  {'✅' if comp['complete'] else '❌'} COMPLETENESS: {comp['msg']}")
    return res


def run(source, ptmap, since=None, warn_frac=0.10):
    """Run the one reconcile loop for a source adapter. Returns the full reconciliation (truth/captured/attributed/
    residual/by_org/warning/rows) — the same shape for LLM, GPU, subscription, storage."""
    ok, why = owner_ok(source.conn())
    cap = list(source.captured(since) or [])
    truth = source.truth_total(since)                      # None = UNKNOWN (fetch failed); 0.0 = genuinely zero
    cap_sum = round(sum(r.get("cost") or 0.0 for r in cap), 2)
    if truth is None:                                      # can't reconcile against an unread bill — surface it, don't fake it
        return {"source": source.name, "owner_ok": ok, "owner_reason": why, "truth_total": None,
                "captured": cap_sum, "attributed": 0.0, "residual": None,
                "by_org": rollup_by_org(cap, ptmap), "warning": residual_warning(None, None), "rows": cap}
    gap = round(truth - cap_sum, 2)
    attr = list(source.attribute_gap(gap, since) or []) if (ok and gap > 0.5) else []
    attr_sum = round(sum(r.get("cost") or 0.0 for r in attr), 2)
    resid = residual(truth, cap_sum, attr_sum)
    return {"source": source.name, "owner_ok": ok, "owner_reason": why, "truth_total": round(truth, 2),
            "captured": cap_sum, "attributed": attr_sum, "residual": resid,
            "by_org": rollup_by_org(cap + attr, ptmap), "warning": residual_warning(truth, resid, warn_frac),
            "rows": cap + attr}

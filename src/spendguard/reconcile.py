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
    non-owner would attribute other tenants' spend to itself. Standalone/unconnected (no conn) reconciles fully."""
    conn = conn or {}
    if conn.get("enabled") and not conn.get("owns_account"):
        return False, "not the account owner (owns_account=false) — the owner connection reconciles the shared-account gap"
    return True, "account owner"


def org_of(project, ptmap):
    return ptmap.get((project or "").lower(), ("", ""))[0] or "(untagged)"


def rollup_by_org(rows, ptmap, cost_key="cost", proj_key="project"):
    """Roll attributed rows up to org via project→org (ptmap). Diagnostic of WHERE the recorded spend landed."""
    by = {}
    for r in rows:
        o = org_of(r.get(proj_key), ptmap)
        by[o] = round(by.get(o, 0.0) + (r.get(cost_key) or 0.0), 2)
    return by


def residual(truth_total, *captured_sums):
    """truth − Σ(captured/attributed). The unrecoverable remainder — SURFACED, never dumped on a project/org."""
    return round((truth_total or 0.0) - sum(captured_sums), 2)


def residual_warning(truth_total, resid, frac=0.10, floor=25.0):
    """A large residual means a source/tenant is UNDER-attributed (destroyed/ungated spend not yet recovered).
    Surface it loudly instead of letting it float or get silently dumped. Returns a message or None."""
    if truth_total and resid > max(floor, frac * truth_total):
        return (f"residual ${resid:.2f} is {resid / truth_total * 100:.0f}% of the account — a source/tenant is "
                "UNDER-attributed; recover its destroyed/ungated spend (or it floats). Durable fix: capture at source.")
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


def run(source, ptmap, since=None, warn_frac=0.10):
    """Run the one reconcile loop for a source adapter. Returns the full reconciliation (truth/captured/attributed/
    residual/by_org/warning/rows) — the same shape for LLM, GPU, subscription, storage."""
    ok, why = owner_ok(source.conn())
    cap = list(source.captured(since) or [])
    truth = source.truth_total(since) or 0.0
    cap_sum = round(sum(r.get("cost") or 0.0 for r in cap), 2)
    gap = round(truth - cap_sum, 2)
    attr = list(source.attribute_gap(gap, since) or []) if (ok and gap > 0.5) else []
    attr_sum = round(sum(r.get("cost") or 0.0 for r in attr), 2)
    resid = residual(truth, cap_sum, attr_sum)
    return {"source": source.name, "owner_ok": ok, "owner_reason": why, "truth_total": round(truth, 2),
            "captured": cap_sum, "attributed": attr_sum, "residual": resid,
            "by_org": rollup_by_org(cap + attr, ptmap), "warning": residual_warning(truth, resid, warn_frac),
            "rows": cap + attr}

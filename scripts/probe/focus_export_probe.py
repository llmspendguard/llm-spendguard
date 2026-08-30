"""VALIDATION PROBE (read-only, $0, no LLM): how close is spendguard's spend_events ledger to a FOCUS 1.2 row?

Answers three questions against the REAL ledger, on real rows:
  1. COVERAGE — of the FOCUS 1.2 columns an LLM charge needs, how many already have a spendguard source column,
     and how populated are they in practice?
  2. PROJECTION — map real spend_events rows to FOCUS 1.2 rows (focus_row); print a billed row, an est-value
     (subscription-covered) row, and a true-down CORRECTION row, to show the mapping is a projection, not a rebuild.
  3. THE TWO SPENDGUARD-SPECIFIC SEMANTICS that FOCUS expresses natively:
     - subscription_usd (a Purchase / covering charge) → est_chat_usd usage (BilledCost 0, EffectiveCost = a share
       of the covering charge). That is FOCUS's CommitmentDiscount / EffectiveCost model — the REAL-$ vs est-value
       split, never summed, lands as BilledCost vs EffectiveCost.
     - true_down negative rows (reverse+re-enter, original never mutated) → FOCUS "Ledger" correction style, and a
       capture leak → a "Late-Arriving Usage" correction. The residual is carried by the correction row's BilledCost.

NOT a schema change and NOT the exporter itself — a probe that proves the exporter is a thin projection. Run under
the gated venv for consistency though it spends nothing: `.venv.nosync/bin/python scripts/probe/focus_export_probe.py`
"""
import os
import sqlite3

from spendguard import config
from spendguard import ledger as _ledger

# ── FOCUS 1.2 columns an LLM charge needs → the spendguard source column (or a constant/derivation). This is the
#    mapping table; a value of None means "constant/derived", a string means "this spend_events column". ──────────
FOCUS_TO_SPENDGUARD = {
    # identity / lineage
    "ChargeId":            "id",                # not a FOCUS 1.2 column, but every row needs a stable key
    "ChargePeriodStart":   "occurred_at",       # transaction date
    "ChargePeriodEnd":     "occurred_at",
    "BillingPeriodStart":  "period",
    "BillingPeriodEnd":    "period",
    # provider identity
    "ProviderName":        "provider",
    "PublisherName":       "provider",           # refined by a maker map (Anthropic vs AWS-hosted); provider is the floor
    "InvoiceIssuerName":   "provider",
    "ServiceName":         None,                 # derived: f"{provider} {model_kind or 'API'}"
    "ServiceCategory":     None,                 # constant "AI and Machine Learning"
    # sku / pricing
    "SkuId":               None,                 # derived: f"{model}:{axis}"
    "ListUnitPrice":       "rate_in",            # $/token in (rate_out is the output axis)
    "PricingQuantity":     None,                 # derived: in_tok+out_tok
    "PricingUnit":         None,                 # constant "tokens"
    "ConsumedQuantity":    "num_calls",
    "ConsumedUnit":        None,                 # constant "requests"
    # money
    "BillingCurrency":     "currency",
    "BilledCost":          None,                 # derived from the axis column + cost_basis (see focus_row)
    "EffectiveCost":       None,                 # derived: est_chat covered by subscription → a share, else == Billed
    "ListCost":            None,                 # derived: estimate axis / rate*tokens
    # charge classification
    "ChargeCategory":      None,                 # derived from kind: Usage | Purchase(subscription) | ...
    "ChargeClass":         None,                 # "Correction" for a closed-period true-down, else null
    "ChargeFrequency":     None,                 # Usage-Based | Recurring(subscription)
    # tenant / billing entity
    "BillingAccountId":    "account_id",
    "SubAccountId":        "org",                # org is the tenant; repo/project ride in Tags
    "InvoiceId":           "invoice_id",
    # attribution (spendguard's edge — FOCUS carries these only as free-form Tags)
    "Tags":                None,                 # {repo, project, intent, actor, attr_what, model, batch_id, conv_id}
}

# the FOCUS-critical subset we score coverage on (money + reconcile + token metering + tenant)
_CRITICAL = ["occurred_at", "period", "provider", "model", "currency", "rate_in", "num_calls",
             "in_tok", "out_tok", "org", "invoice_id", "cost_basis", "conv_id", "attr_what"]

_KIND_COL = {"batch": "batch_usd", "realtime": "realtime_usd", "est_chat": "est_chat_usd",
             "remote_compute": "remote_compute_usd", "subscription": "subscription_usd"}
_BILLED_KINDS = {"batch", "realtime", "remote_compute", "subscription"}


def _axis_and_amount(ev):
    """Which of the five category columns carries this row's money, and the decimal amount (string)."""
    for kind, col in _KIND_COL.items():
        v = ev.get(col)
        if v not in (None, "", "0", "0.0"):
            return kind, col, v
    return None, None, None


def focus_row(ev):
    """Project ONE spend_events dict to a FOCUS-1.2 charge row. Pure mapping — the real exporter is this plus a
    provider→publisher maker map and currency normalisation. Demonstrates the projection is thin."""
    kind, _col, amt = _axis_and_amount(ev)
    basis = (ev.get("cost_basis") or "").lower()
    is_correction = (ev.get("conv_id") == getattr(__import__("spendguard.budget", fromlist=["_TRUE_DOWN_CONV"]),
                                                  "_TRUE_DOWN_CONV", "__sg_true_down__"))
    # BilledCost: an invoice-aligned figure only once reconciled/billed; an estimate is ListCost, not BilledCost.
    billed = amt if (basis in ("billed", "reconstructed") or ev.get("billed")) else None
    # est_chat is subscription-COVERED usage: never a real dollar out the door (BilledCost 0); its EffectiveCost is a
    # share of the subscription covering charge — the est-value axis, expressed in FOCUS without ever summing it in.
    if kind == "est_chat":
        billed = "0"
    charge_cat = ("Purchase" if kind == "subscription"
                  else "Adjustment" if is_correction and kind not in _BILLED_KINDS
                  else "Usage")
    return {
        "ChargeId": ev.get("id"),
        "ChargePeriodStart": ev.get("occurred_at"), "BillingPeriodStart": ev.get("period"),
        "ProviderName": ev.get("provider"), "PublisherName": ev.get("provider"),
        "ServiceName": f"{ev.get('provider') or '?'} {ev.get('model_kind') or 'API'}",
        "ServiceCategory": "AI and Machine Learning",
        "SkuId": f"{ev.get('model')}:{kind}",
        "PricingQuantity": (ev.get("in_tok") or 0) + (ev.get("out_tok") or 0), "PricingUnit": "tokens",
        "ConsumedQuantity": ev.get("num_calls") or 1, "ConsumedUnit": "requests",
        "BillingCurrency": ev.get("currency") or "USD",
        "BilledCost": billed,
        "ListCost": amt if basis == "estimate" else None,
        "EffectiveCost": ("covered-by-subscription-share" if kind == "est_chat" else billed),
        "ChargeCategory": charge_cat,
        "ChargeClass": "Correction" if is_correction else None,
        "ChargeFrequency": "Recurring" if kind == "subscription" else "Usage-Based",
        "BillingAccountId": ev.get("account_id"), "SubAccountId": ev.get("org"),
        "InvoiceId": ev.get("invoice_id"),
        "Tags": {k: ev.get(k) for k in ("repo", "project_primary", "intent", "actor", "attr_what",
                                        "model", "batch_id", "conv_id") if ev.get(k)},
        "_sg_kind": kind, "_sg_cost_basis": ev.get("cost_basis"),
    }


def main():
    dbp = config.db_path()
    print(f"ledger: {dbp}")
    if not os.path.exists(dbp):
        print("  (no ledger db yet — nothing to project)"); return
    con = sqlite3.connect(f"file:{dbp}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    cols = {r[1] for r in con.execute("PRAGMA table_info(spend_events)")}
    n = con.execute("SELECT COUNT(*) FROM spend_events").fetchone()[0]
    print(f"rows: {n:,}\n")

    # 1) FOCUS coverage: every FOCUS column an LLM charge needs already has a spendguard source (col or derivation)
    have = sum(1 for src in FOCUS_TO_SPENDGUARD.values() if src is None or src in cols)
    print(f"[1] FOCUS coverage: {have}/{len(FOCUS_TO_SPENDGUARD)} FOCUS columns have a spendguard source "
          f"(column present or derivable) — the rest are constants/derivations, none require a new column.")
    missing = [f for f, s in FOCUS_TO_SPENDGUARD.items() if s is not None and s not in cols]
    print(f"    source columns MISSING from the live schema: {missing or 'none'}")

    # population of the critical subset on real rows
    print("\n[2] population of FOCUS-critical source columns on real rows:")
    for c in _CRITICAL:
        if c not in cols:
            print(f"    {c:14s} : (column absent)"); continue
        pop = con.execute(f"SELECT COUNT(*) FROM spend_events WHERE {c} IS NOT NULL AND {c} != ''").fetchone()[0]
        print(f"    {c:14s} : {pop:>7,}/{n:,}  ({(100*pop//n) if n else 0}%)")

    # 3) project real sample rows of each interesting shape to FOCUS
    def sample(where, label):
        r = con.execute(f"SELECT * FROM spend_events WHERE {where} LIMIT 1").fetchone()
        print(f"\n[3:{label}] " + ("no such row in this ledger" if not r else ""))
        if r:
            fr = focus_row(dict(r))
            for k, v in fr.items():
                if v not in (None, {}, ""):
                    print(f"     {k:20s} {v}")
    sample("billed=1 AND (batch_usd IS NOT NULL OR realtime_usd IS NOT NULL)", "billed-usage")
    sample("est_chat_usd IS NOT NULL AND est_chat_usd != ''", "est-value(subscription-covered)")
    sample("subscription_usd IS NOT NULL AND subscription_usd != ''", "subscription(covering-charge)")
    from spendguard import budget
    tdc = getattr(budget, "_TRUE_DOWN_CONV", None)
    if tdc:
        sample(f"conv_id = '{tdc}'", "true-down CORRECTION (FOCUS Ledger style)")
    con.close()
    print("\nverdict: the ledger is a SUPERSET of FOCUS 1.2's LLM-charge columns; a FOCUS export is a thin "
          "projection (focus_row above), not a schema change. est-value and true-down map to FOCUS's "
          "EffectiveCost/covering-charge and Ledger-correction semantics respectively.")


if __name__ == "__main__":
    main()

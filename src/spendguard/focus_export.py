"""FOCUS export — project spend_events into FinOps FOCUS 1.2 charge rows so spendguard's ledger is ingestible by
any FinOps stack, and spendguard reads as an early FOCUS-for-LLM reference implementation (no ratified LLM profile
exists yet — the FinOps Token Economics WG called it "natural and necessary" but shipped no schema).

This is a PROJECTION, not a second ledger: it reads the one money-of-record (spend_events) and maps each row to the
FOCUS columns an LLM charge needs. The mapping was validated on the real ledger — every FOCUS column an LLM charge
needs already has a spendguard source; no new ledger column was added for it (see scripts/probe/focus_export_probe.py).

Two spendguard-specific semantics land in FOCUS natively, and NEITHER is ever summed into the other:
  • the subscription -> est-value split is FOCUS's covering-charge -> EffectiveCost model: the flat plan fee is a
    Purchase; plan-covered usage (est_chat) is a covered charge with BilledCost 0 and EffectiveCost = its share.
  • true_down negative rows (reverse + re-enter, original never mutated) are FOCUS's "Ledger" correction style;
    ChargeClass="Correction". A capture leak is a Late-Arriving Usage correction.

InvoiceId is emitted ONLY where the reconcile path stamped one (a provider:period reference) — an unreconciled
estimate row honestly carries ListCost with NO InvoiceId, never a BilledCost it was never billed for.

No LLM, no network, read-only. `spendguard focus-export [--since DATE] [--until DATE] [--format json|csv] [--out F]`.
"""
import csv
import io
import json
import sys

from . import budget

# The FOCUS 1.2 columns an LLM charge row needs, in a stable emit order. ChargeId is a spendguard convenience (FOCUS
# 1.2 has no ChargeId column); everything else is a real FOCUS column. Kept as a named list so CSV headers and JSON
# keys agree and a reader can diff two exports column-for-column.
FOCUS_COLUMNS = (
    "ChargeId", "ChargePeriodStart", "ChargePeriodEnd", "BillingPeriodStart",
    "ProviderName", "PublisherName", "InvoiceIssuerName", "ServiceName", "ServiceCategory",
    "SkuId", "ChargeCategory", "ChargeClass", "ChargeFrequency",
    "PricingQuantity", "PricingUnit", "ConsumedQuantity", "ConsumedUnit",
    "BillingCurrency", "ListCost", "ContractedCost", "BilledCost", "EffectiveCost",
    "BillingAccountId", "SubAccountId", "InvoiceId", "Tags",
)

_SERVICE_CATEGORY = "AI and Machine Learning"

# spend kind -> the one category column that carries its money. Mirrors ledger._KIND_TO_USD but names only the five
# categories an export cares about (batch/realtime are billed usage; est_chat is subscription-covered; subscription
# is the covering charge; remote_compute is GPU/box). Sourced from the ledger's own map so it can't drift.
_KIND_COL = {"batch": "batch_usd", "realtime": "realtime_usd", "est_chat": "est_chat_usd",
             "remote_compute": "remote_compute_usd", "subscription": "subscription_usd"}
_BILLED_KINDS = frozenset(("batch", "realtime", "remote_compute", "subscription"))
_TAG_KEYS = ("repo", "project_primary", "intent", "actor", "attr_what", "model", "batch_id", "conv_id", "team")


def _charge_axis(ev):
    """(kind, amount_str) — which of the five category columns carries this row's money, and its exact-decimal string.
    None, None when the row carries no money (an explicit unpriced row)."""
    for kind, col in _KIND_COL.items():
        v = ev.get(col)
        if v not in (None, "", "0", "0.0"):
            return kind, v
    return None, None


def focus_row(ev):
    """Project ONE spend_events dict into a FOCUS 1.2 charge row (a dict keyed by FOCUS_COLUMNS). Pure mapping."""
    kind, amt = _charge_axis(ev)
    basis = (ev.get("cost_basis") or "").lower()
    is_correction = ev.get("conv_id") == budget._TRUE_DOWN_CONV
    provider = ev.get("provider") or "?"

    # BilledCost is invoice-aligned. Only an explicit ESTIMATE (a pre-submit projection) is ListCost-not-billed;
    # everything else that carries money is actual/billed spend (a live realtime charge, a reconciled row, a
    # subscription fee, a correction). Do NOT read the ledger's `billed` flag — it DEFAULTS to 1 on every row, so it
    # cannot distinguish an estimate from an actual and would mislabel every estimate as billed.
    if basis == "estimate":
        list_cost, billed = amt, None
    else:
        list_cost, billed = None, amt
    if kind == "est_chat":                              # subscription-COVERED usage: no $ out the door; its value is
        billed, effective = "0", "covered-by-subscription"   # EffectiveCost = a share of the covering charge
    else:
        effective = billed

    charge_cat = ("Purchase" if kind == "subscription"
                  else "Adjustment" if (is_correction and kind not in _BILLED_KINDS)
                  else "Usage")
    return {
        "ChargeId": ev.get("id"),
        "ChargePeriodStart": ev.get("occurred_at"), "ChargePeriodEnd": ev.get("occurred_at"),
        "BillingPeriodStart": ev.get("period"),
        "ProviderName": provider, "PublisherName": provider, "InvoiceIssuerName": provider,
        "ServiceName": "%s %s" % (provider, ev.get("model_kind") or "API"),
        "ServiceCategory": _SERVICE_CATEGORY,
        "SkuId": "%s:%s" % (ev.get("model") or "?", kind or "?"),
        "ChargeCategory": charge_cat,
        "ChargeClass": "Correction" if is_correction else None,
        "ChargeFrequency": "Recurring" if kind == "subscription" else "Usage-Based",
        "PricingQuantity": (ev.get("in_tok") or 0) + (ev.get("out_tok") or 0), "PricingUnit": "tokens",
        "ConsumedQuantity": ev.get("num_calls") or 1, "ConsumedUnit": "requests",
        "BillingCurrency": ev.get("currency") or "USD",
        "ListCost": list_cost, "ContractedCost": None, "BilledCost": billed, "EffectiveCost": effective,
        "BillingAccountId": ev.get("account_id"), "SubAccountId": ev.get("org"),
        "InvoiceId": ev.get("invoice_id") or None,      # stamped only on reconciled rows — honest by construction
        "Tags": {k: ev.get(k) for k in _TAG_KEYS if ev.get(k)},
    }


def export_rows(since=None, until=None):
    """Every spend_events row in the window, projected to FOCUS. Read-only, through the shared ledger connection."""
    return [focus_row(ev) for ev in budget._ledger().query(since=since, until=until)]


def reconciliation_summary(rows):
    """The cross-check a FinOps reader needs: how much of the export is invoice-anchored, and the money on each FOCUS
    axis (never summed across axes — BilledCost and est-value stay apart). $ figures are exact-decimal strings."""
    from decimal import Decimal, ROUND_HALF_UP
    _MICRO = Decimal("0.000001")                          # the ledger's micro-dollar precision; a summary shows money,
    def _sum(col):                                        # not the long float tails that accumulate over 20k+ rows
        t = Decimal(0)
        for r in rows:
            v = r.get(col)
            if v not in (None, "", "covered-by-subscription"):
                try:
                    t += Decimal(str(v))
                except Exception:
                    pass
        return str(t.quantize(_MICRO, rounding=ROUND_HALF_UP))
    invoiced = sum(1 for r in rows if r.get("InvoiceId"))
    corrections = sum(1 for r in rows if r.get("ChargeClass") == "Correction")
    return {
        "rows": len(rows),
        "billed_cost": _sum("BilledCost"),            # provider-billed / reconciled $ out the door
        "list_cost": _sum("ListCost"),                # estimate $ (not yet billed)
        "rows_invoice_anchored": invoiced,
        "correction_rows": corrections,
        "est_value_rows": sum(1 for r in rows if r.get("EffectiveCost") == "covered-by-subscription"),
    }


def emit_csv(rows):
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=list(FOCUS_COLUMNS), extrasaction="ignore")
    w.writeheader()
    for r in rows:
        row = dict(r)
        row["Tags"] = json.dumps(row.get("Tags") or {}, separators=(",", ":"))   # FOCUS Tags = a JSON-string column
        w.writerow(row)
    return buf.getvalue()


def emit_json(rows):
    return json.dumps(rows, indent=2, default=str)


def main(argv=None):
    argv = list(argv if argv is not None else sys.argv[1:])
    def _opt(name, default=None):
        return argv[argv.index(name) + 1] if name in argv and argv.index(name) + 1 < len(argv) else default
    since, until = _opt("--since"), _opt("--until")
    fmt = (_opt("--format", "json") or "json").lower()
    out = _opt("--out")

    rows = export_rows(since=since, until=until)
    summ = reconciliation_summary(rows)
    body = emit_csv(rows) if fmt == "csv" else emit_json(rows)

    if out:
        with open(out, "w") as f:
            f.write(body)
        sys.stderr.write("focus-export: wrote %d FOCUS rows -> %s\n" % (summ["rows"], out))
    else:
        sys.stdout.write(body + ("\n" if not body.endswith("\n") else ""))
    # the cross-check, to stderr so it never pollutes a piped export: what's billed, what's anchored to an invoice.
    sys.stderr.write(
        "focus-export: %(rows)d rows -> BilledCost $%(billed_cost)s (List $%(list_cost)s) -> "
        "%(rows_invoice_anchored)d invoice-anchored -> %(correction_rows)d corrections -> "
        "%(est_value_rows)d subscription-covered (est-value, never summed into billed)\n" % summ)
    return 0


if __name__ == "__main__":
    sys.exit(main())

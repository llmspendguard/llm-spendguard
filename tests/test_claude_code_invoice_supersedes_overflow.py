"""Guard: a provider INVOICE is the actual-$ TRUTH and SUPERSEDES the observable overflow ESTIMATE it covers —
so subscription-lane overage is NEVER double-counted in the actual-$ roll-up push. GENERAL across lanes (matched
by provider+month), not hardcoded to claude-code/anthropic. Pins:
  1. Before reconcile, both the overflow estimate AND the invoice sit in the actual-$ total → a double-count.
  2. _supersede_overflow_for_invoiced_months() marks each INVOICED (provider, month)'s overflow billed=0 (out of
     the actual-$ total by_dims feeds the server), while an UN-invoiced month keeps its overflow as actual-$.
  3. It works for a SECOND lane (codex/openai) with ZERO new code — the same reconcile supersedes it too.
  4. The overflow rows are NOT deleted — overflow_by_conversation still reads them for per-conversation attribution.
  5. Idempotent: a second run supersedes nothing more.
Hermetic: isolated SPENDGUARD_HOME + seeded ledger rows across two lanes/providers, zero spend."""
import os
import sys
import tempfile

if not os.environ.get("SPENDGUARD_TEST_ISOLATED"):
    os.environ["SPENDGUARD_TEST_ISOLATED"] = "1"
    os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="spendguard-ovinv-")
    os.execv(sys.executable, [sys.executable] + sys.argv)

from spendguard import claudecode, budget

fails = []


def ck(name, cond):
    print(("  [OK] " if cond else "  [FAIL] ") + name)
    if not cond:
        fails.append(name)


def _actual_usd():
    """The actual-$ total by_dims would push (billed rows only, in the isolated home = just our seeds)."""
    return round(sum(r["cost"] for r in budget.by_dims()), 2)


# LANE 1 — claude-code / anthropic: overflow estimate in an invoiced month (Aug) + un-invoiced month (Sep), Aug invoice
budget._record_spend_event("anthropic", "claude-opus-4-8", "realtime", 40.0, conv_id="cAug", project="claude-code",
                           occurred_at="2026-08-15T00:00:00+00:00", source="claude-code-overflow",
                           intent="claude-code:overage", dedup_key="of:aug:cAug")
budget._record_spend_event("anthropic", "claude-opus-4-8", "realtime", 5.0, conv_id="cSep", project="claude-code",
                           occurred_at="2026-09-02T00:00:00+00:00", source="claude-code-overflow",
                           intent="claude-code:overage", dedup_key="of:sep:cSep")
budget._record_spend_event("anthropic", "claude-opus-4-8", "realtime", 59.0, conv_id="", project="claude-code",
                           occurred_at="2026-08-31T00:00:00+00:00", source="anthropic-invoice",
                           intent="anthropic-invoice:cc-overage", dedup_key="inv:aug")
# LANE 2 — codex / openai: a DIFFERENT provider's overflow (Aug) + its own invoice (Aug). Same reconcile, no new code.
budget._record_spend_event("openai", "gpt-5.6-sol", "realtime", 30.0, conv_id="cOAug", project="codex",
                           occurred_at="2026-08-20T00:00:00+00:00", source="codex-overflow",
                           intent="codex:overage", dedup_key="of:oai:aug")
budget._record_spend_event("openai", "gpt-5.6-sol", "realtime", 28.0, conv_id="", project="codex",
                           occurred_at="2026-08-31T00:00:00+00:00", source="openai-invoice",
                           intent="openai-invoice:overage", dedup_key="inv:oai:aug")
# A REFUND/correction is NOT an authoritative overage charge — it must NOT supersede an estimate. Oct has an overflow
# estimate + only a refund row (stream 'overage-refund', not 'cc-overage'/'overage') → the estimate must SURVIVE.
budget._record_spend_event("anthropic", "claude-opus-4-8", "realtime", 3.0, conv_id="cOct", project="claude-code",
                           occurred_at="2026-10-05T00:00:00+00:00", source="claude-code-overflow",
                           intent="claude-code:overage", dedup_key="of:oct:cOct")
budget._record_spend_event("anthropic", "claude-opus-4-8", "realtime", -1.0, conv_id="", project="claude-code",
                           occurred_at="2026-10-31T00:00:00+00:00", source="anthropic-invoice",
                           intent="anthropic-invoice:overage-refund:card1955:reimb-no", dedup_key="inv:oct:refund")

ck("BEFORE: every overflow estimate AND invoice sits in actual-$ — double-count "
   "($40+$5+$59 + $30+$28 + $3-$1 = $164)", _actual_usd() == 164.0)

n = claudecode._supersede_overflow_for_invoiced_months()
ck("reconcile superseded ONLY the two invoiced-month overflows — anthropic Aug AND openai Aug (general, 2 rows)", n == 2)
ck("AFTER: actual-$ = invoices $59+$28 + un-invoiced Sep $5 + Oct $3 + refund -$1 = $94 (no double-count)",
   _actual_usd() == 94.0)

led = budget._ledger()
cc_month = dict(led.source_rows("claude-code-overflow", cols=("month", "billed")))
codex_month = dict(led.source_rows("codex-overflow", cols=("month", "billed")))
ck("lane 1 (anthropic): the INVOICED month's overflow is billed=0, kept for attribution", cc_month.get("2026-08") == 0)
ck("lane 1: the UN-invoiced month keeps its overflow at billed=1 (best actual-$ until its invoice arrives)",
   cc_month.get("2026-09") == 1)
ck("lane 2 (openai/codex): its invoiced overflow is ALSO superseded → billed=0 (generality, no per-lane code)",
   codex_month.get("2026-08") == 0)
ck("a REFUND row does NOT count as authoritative overage — Oct overflow is NOT superseded (stays billed=1)",
   cc_month.get("2026-10") == 1)

obc = claudecode.overflow_by_conversation()
ck("overflow rows are NOT deleted — claude-code attribution still reads ALL its months ($48 shape)",
   round(sum(obc.values()), 2) == 48.0 and set(obc) == {"cAug", "cSep", "cOct"})

ck("idempotent: a second reconcile supersedes nothing more", claudecode._supersede_overflow_for_invoiced_months() == 0)

# the FACTUAL per-provider running tally — invoice truth + un-invoiced estimate, general across providers, no double-count
tally = claudecode.overage_tally()
ck("overage_tally (anthropic): total $67 = invoice $59 + un-invoiced est $8 (Sep+Oct); the refund is NOT counted",
   round(tally.get("anthropic", {}).get("total_usd", 0), 2) == 67.0
   and tally["anthropic"]["invoiced_usd"] == 59.0 and tally["anthropic"]["estimate_usd"] == 8.0)
ck("overage_tally (openai/codex): total $28 = invoice $28, no un-invoiced estimate — GENERAL per provider",
   round(tally.get("openai", {}).get("total_usd", 0), 2) == 28.0 and tally["openai"]["estimate_usd"] == 0.0)

print(("\n[OK] " if not fails else "\n[FAIL] ") + "claude_code_invoice_supersedes_overflow: %d failure(s)" % len(fails))
sys.exit(1 if fails else 0)

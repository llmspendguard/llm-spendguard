---
name: spend
description: Quick spendguard status — current LLM spend (today/7d/month), the ledger-leak check, and the top cost learnings. Use when the user asks "how much have we spent", "current LLM cost / token spend", "what are our cost learnings", or wants a fast spendguard readout. Read-only, no LLM spend.
---

# spend — quick spendguard status

Run these read-only commands (no LLM spend) and summarize for the user — lead with today's total, flag any
leak alert, then the top learnings:

```
spendguard report            # today / 7d / month totals + ledger-leak alert + top learnings
spendguard insights list     # all confidence-scored learnings
spendguard calls             # $/good-result per intent (if the call corpus is enabled)
```

Follow-ups the user may want:
- **Plan a new task:** `spendguard brief --task "<what they want to do>"` — pre-filled confirm/correct plan.
- **Per-intent recommendation:** `spendguard optimize --intent <X>` (caged, estimate-first — shows cost before spending).
- **Find ungoverned spend:** `spendguard reconcile-ledger`.

Keep the summary tight: $ today/7d/month, leak (if any), and the 3 most actionable learnings.

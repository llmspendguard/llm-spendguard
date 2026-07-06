---
name: spendguard-close
description: Run the monthly close — push provider-truth totals (account owner only), produce the client close view, and read the org server's statement (ledger vs provider truth with the residual named per provider). Use when the user says "close the month", "monthly statement", "reconcile the month", or asks how the month's spend squares against provider bills. Zero model spend.
---

# spendguard-close — the monthly close

The accountant's ritual: local provider truth → synced to the org server → the statement shows
ledger-vs-truth with the residual NAMED per provider. All steps zero model spend.

## 1. Push provider truth (account owner only)
```
spendguard truth              # preview: per-day per-provider totals from the providers' own APIs
spendguard truth --push       # sync {day, provider, usd} to the org server (keys never leave this machine)
```
If it prints `skipped: not the account owner` that is CORRECT — provider truth is ACCOUNT-level and only
the `owns_account: true` connection may claim it (a non-owner push would double-count truth and corrupt
both orgs' residuals). Tell the user which repo/machine owns the account and run it there.
(Daily `saas sync` already pushes this automatically; the manual push is for closing right now.)

## 2. Client close view
```
spendguard close [--month YYYY-MM] [--csv close.csv]
```
Prints per-provider month totals (+ the leak line for the open month). Default = current month.

## 3. The org statement (the real artifact)
Point the user at **https://<their-server>/statements** (llmspendguard.com/statements), signed into the
OWNING org — real-$ by class / project / team, prior-month delta, YTD closing, and the truth table.
CSV: `/api/statement?month=YYYY-MM&format=csv`.

## 4. Read the residual honestly
- `residual > 0` = the provider billed money the org ledger hasn't attributed → run `/spendguard-reconcile`.
- **UNKNOWN** (no truth rows) = the owner hasn't pushed truth for that month — never read it as zero.
- On a shared provider account, the owning org's residual includes other orgs' spend on that account —
  small and documented, but say it out loud when interpreting.
Close by stating: month real total, delta vs prior, YTD, and per-provider residual with the next action.

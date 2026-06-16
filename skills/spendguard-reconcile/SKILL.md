---
name: spendguard-reconcile
description: Make the local spendguard ledger TRUE — reconcile it against actual OpenAI/Anthropic provider billing, surface cost-capture leaks (provider-billed spend the gate never recorded), review conversations for attribution context, and push the reconciled truth to the team/org dashboard. Use when spend numbers look wrong/low, the dashboard disagrees with provider bills, or the user wants to verify capture is complete. Reconcile + leak-check are zero model spend.
---

# spendguard-reconcile — trust the number

The gate ledger only captures spend that passed through the gate. Provider billing is the ground truth. When
they disagree (dashboard low, "where did $X go"), reconcile so the LOCAL ledger reflects provider truth and the
gap (ungoverned / pre-ledger / non-gated) is explicit.

## 1. Reconcile the ledger to provider truth (free)
```
spendguard saas reconcile      # writes the per-(provider,day) gap as 'unattributed' rows → ledger = provider total
spendguard reconcile-ledger    # the leak view: provider batch vs local, per day, since the ledger went live
```
Read the result: `provider_total`, `gate_attributed`, `ungoverned`, `coverage %`, and **`errors` / `providers_ok`**
— if a provider is in `errors`, the fetch was PARTIAL; do not trust the total until it's clean (check the API key
is resolvable, not just present in the shell — it loads from `.env`/config). A silent partial fetch is the classic
trust bug; this surfaces it.

## 2. Identify cost-capture leaks
- `ungoverned > 0` (coverage < ~100%) = provider billed spend the gate never saw → a repo/venv running **non-gated**,
  or spend from **before** the local ledger existed. Action: install the gate there (`spendguard install-hook`),
  then re-reconcile.
- Real-time spend needs a provider **Admin key** to see across hosts (batch is free to fetch). Without it, only
  this venv's real-time is visible — note that gap explicitly.

## 3. Review conversations for attribution context
```
spendguard conv <transcript-or-dir>   # mine Claude Code/chat transcripts: decisions, outcomes, which work/project
```
Use this to attribute ungoverned spend to the right project/user and to sanity-check that the model/intent in the
ledger matches what actually happened.

## 4. Push the reconciled truth
```
spendguard saas push           # sends the reconciled ledger (attributed + 'unattributed' gap + llmseg) to the org
spendguard saas sync           # reconcile + push + run any server-queued reconcile/re-tag in one step
```
The dashboard then shows the real total, a **Governed %** (attributed ÷ total), and the ungoverned $ to chase.

## Principle
The number must be **correct, current, and reconciled** — otherwise it's not trustworthy. Reconcile before
reporting; never present the gate ledger alone as "spend" when provider billing is available.

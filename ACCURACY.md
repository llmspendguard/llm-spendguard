# Accuracy: how close are these numbers to your actual bill?

Every LLM cost tool prints dollars. Almost none will tell you how wrong those dollars are. Surveying the field in
July 2026, **not one** of the major observability or gateway platforms publishes an error rate against a provider
invoice — the best of them tell you to check a sample day by hand, and one states plainly that "your cost estimate
is silently wrong."

We think a tool that prints money owes you a number. Here is ours, how it is measured, and — more importantly —
what it does **not** cover.

---

## The short answer

| What | Measured delta vs provider truth | How |
|---|---|---|
| **Batch spend, reconciled** | **exact** (to the cent, on the last verified run) | per-batch actuals from the provider's own batch API |
| **Realtime spend, reconstructed** | **+4.4%** over a 2-month window | conversational reconstruction vs the provider's admin usage API |
| **Unpriced models** | **never estimated** | the call fails loud; we do not guess a price |

Two different mechanisms, two different confidence levels, and we label which one produced any given number.

## Worked example — a real reconcile, reproducible

From a live run on 2026-07-16 (Anthropic, month-to-date):

```
provider batch billing (their API)      $107.67
  ├─ gate-recorded + attributed         $105.77
  └─ residual, named as ungoverned        $1.90     ← 98.2% captured at the source
trust check                              recorded ≈ billed (+6%), 🟢
```

The residual is not rounding — it is spend the gate never saw (a process running outside a gated interpreter),
and naming it is the point. A tool that quietly absorbs that gap into "your spend" is lying to you comfortably.

Reproduce it on your own account:

```bash
spendguard reconcile all       # local ledger vs provider billing, per provider, per day
spendguard trust               # is what we recorded ≈ what you were billed?
```

Both are free — they read the providers' billing endpoints, which cost nothing, and make no model calls.

## Why batch is exact and realtime is not

**Batch** is exact because we don't estimate it after the fact: the provider's batch API returns the actual token
counts per batch, and we price those with the same table used everywhere else, then **true the estimate down to
the billed number** (`ledger_sync.true_down`). The gate's pre-submit figure is a *ceiling* — it assumes every
request runs to its `max_tokens` — so before true-down the ledger reads high; after, it reads billed.

**Realtime** has no equivalent per-call receipt without an admin key, so it is reconstructed from what the gate
captured at call time plus, for historical spend, an agentic pass over conversations. Validated over a two-month
window against the provider's admin usage API: **$3,020 reconstructed vs $2,891.95 actual = +4.4%**. The
printed-dollar backbone alone (runs whose cost was stated in the transcript) was **96%** of actual; the rest is
estimated and deliberately discounted.

## What we do NOT capture — read this part

- **Ungated processes.** The gate only sees interpreters it is loaded into. Anything else shows up as the
  *residual* above, never silently as $0. `spendguard coverage` lists interpreters that can spend but aren't gated.
- **Realtime spend on other machines** without a provider admin key. There is no free per-call source for it; we
  say UNKNOWN rather than infer.
- **Unpriced models.** If our table and the synced breadth layer both lack a model, the call raises rather than
  billing at $0. An unknown model must not become an uncapped model — a real competitor documents exactly that
  failure mode, where a $0 price also silently exempts the call from the budget.
- **In-flight batches.** Submitted but unbilled work shows as the estimate ceiling until the provider bills it;
  the next reconcile trues it down.
- **Tiered / context-length pricing.** Providers do not tell you in the response which tier you hit. Where a model
  has context-tiered rates we price the base tier, which can under-count long-context calls.
- **Subscription-covered usage** (Claude Max, ChatGPT Pro) is **never** counted as billed dollars. It appears on a
  separate est-value axis, and the two are never summed. If a subscription figure is an assumed default rather
  than your configured plan, it is marked with `*` and says so.

## How pricing itself stays honest

Rates come from a curated, source-attributed table, layered under a synced breadth layer (~2,500 models) that
refreshes daily. A price is never hardcoded in code. When two vendors publish different rates for the same model
id, we raise and ask you to name the vendor rather than pick one. When we have no rate, we fail loud.

## Corrections

If you reconcile and the numbers don't hold up on your account, that is a bug and we want it: open an issue with
the output of `spendguard reconcile all` and `spendguard trust` (neither includes prompts, outputs, or keys).

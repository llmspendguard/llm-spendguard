# Launch plan — what to post, where, and when

Written 2026-07-16 against a survey of what actually worked and what died in this category. It is deliberately
conservative: LLM-cost Show HNs are a graveyard, and the ones that landed had a **specific measured number** and
an **agent** framing, not a platform pitch.

## The evidence this is built on

Four days before this was written, *"Show HN: LLM-spend — Audit your OpenAI/Anthropic API spend locally"* got
**1 point, 0 comments** — and it was honest, well-written, and did a provider cross-check within 1%. Its peers:
Costly 4 points, AgentCost 3, Cost-Xray 2, SpendScope 1. Meanwhile, in the same window:

| Post | Points | Why it worked |
|---|---|---|
| Claude Code Usage Monitor | 245 | "dodge usage cut-offs" — a problem you feel *today* |
| Lowfat | 156 | "saved 91.8% of my tokens" — one number |
| CodeBurn | 112 | "$1,400/week with no visibility… **56% of spend was conversation turns, 21% actual coding**" |

The pattern is not "cost tool." It is **a number that reframes something the reader already worries about.**

## The lede

Lead with the incident, because it is true, specific, and instantly recognizable:

> A day of "cost-conscious" LLM work that I expected to cost **~$33** cost **$149.76**. Two causes: a price
> constant hardcoded at the old model's rate, and jobs running one item per request so a shared prompt was
> re-billed on every call. Neither showed up until the invoice.

Then the finding that generalizes — from our own data, and the one to lead the post with:

> The gate's own pre-submit estimate is a **ceiling**: it assumes every request runs to its `max_tokens`. Measured
> against real completions, output fill is **~40–55%** — so a batch that billed $0.60 was "estimated" at $16.
> Every cost tool I checked prints a number like that with no error bar. **None of them publishes an accuracy
> figure at all.**

That is the hook: *your cost tooling is confidently wrong and nobody measures it.*

## The claim, and the proof that must ship with it

Post **one** measured claim and the harness that produced it. Lowfat posted 91.8% with no end-to-end benchmark and
got dismantled in the comments; Forge (687 points) published its eval harness and went unchallenged.

Our claim: **batch reconciles exact; realtime measured at +4.4% over two months** — with
[ACCURACY.md](ACCURACY.md) linked in the post, including the reproduce-it commands and the explicit
*what we do not capture* list. Never post a savings percentage without the equivalence method beside it.

## What NOT to lead with

- **"Pre-spend gate."** Providers now hard-cap natively at org/project level, every gateway does budgets, and an
  industry roundup already calls pre-request budget enforcement *table stakes*. Three other projects use this
  exact pitch — one of them owns the bare `spendguard` name on PyPI.
- **Subscription-value or coding-agent attribution.** ccusage (~17.6k stars, `npx`, zero config) owns it, free,
  and does it deterministically. We keep the feature; we don't headline it.
- **"2,500 models."** It's a free MIT file everyone syncs.
- **Anything that sounds like a platform.** "Gate + pricing + reconcile + advisor + calibration + experiments +
  receipts + SaaS" reads as unfocused to a stranger and buries the one novel thing.

## Sequence

1. **Ready first** (all done as of v0.8.1): `uvx --from llm-spendguard spendguard scan` works with no key on a
   machine you don't own · `spendguard run --` gates without writing startup hooks · ACCURACY.md published ·
   README under 14K · repo has a description, topics, and a real `--help`.
2. **r/LocalLLaMA** — disclose affiliation, no link in the title, lead with the 27× finding and the fill-rate
   data. This audience checks claims and rewards method.
3. **`awesome-llmops` PR** (5.9k stars, maintained) — one line, accurate category.
4. **Show HN** last, once 2–3 people who aren't you have run it. Title the *finding*, not the tool. Treat it as a
   48-hour pulse: ~92% of the stars a post will generate arrive in that window, at roughly 1.4 stars per upvote —
   so a modest score is a normal outcome, not a verdict on the work.
5. **Sustained** — the interesting posts are the reconciliation findings themselves ("here's what our ledger said
   vs what the invoice said, and why they differed"). That's a series only we can write.

## Honest expectations

Most launches in this category get single-digit points. The realistic goal for week one is **three strangers
running `scan` and one filing a real issue** — that is the signal that the first-run path works. Stars are a lagging
vanity metric; a reconciliation bug report from someone else's account is the leading one.

The State of FinOps 2026 survey has granular AI spend monitoring at token/request/GPU level as its **#1
most-requested capability**, and AI spend management went from 31% to 98% of FinOps teams in two years. Demand is
real. The framing is what's contested — so lead with the measurement nobody else publishes.

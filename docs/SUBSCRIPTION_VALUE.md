# Getting the most from your subscriptions — declare your plans + value policy

You pay a flat fee for each coding subscription (Claude Max, ChatGPT/Codex, Gemini, a GLM Coding plan, …). Each
plan gives you an **allowance that resets on a clock** — weekly tokens, daily tokens, N prompts per window — and
**anything you don't use by the reset is gone**. The flat fee is sunk whether you burn 100% or 3%.

So the value question is not "which model is cheapest per token" (a plan token you already paid for is free). It is:

> **Use every plan to ~100% of its window, and never over — so nothing is wasted and nothing runs out early.**

spendguard does this automatically once you *declare your plans and a per-plan value policy*. Nothing below is
specific to one person's setup — it's all config: your plans, your fees, your policy. This page is how you fill it in.

---

## The one idea: PACE

For each plan, spendguard measures two fractions over the current window:

- **elapsed** — how far through the window the clock is (e.g. day 5 of a 7-day week → 0.71).
- **used** — how much of the allowance you've spent (from the plan's own gauge).

```
pace = elapsed − used
```

- **pace > 0 → BEHIND** — you've spent *less* than the clock. There's paid budget that will expire unused. Route
  discretionary work here to cash it in before reset.
- **pace < 0 → AHEAD** — you're spending *faster* than the clock. At this rate the plan runs out before reset.
  Ease off (or, if you mark it `protect`, shed discretionary work off it entirely).
- **pace ≈ 0 → on pace** — right where it should be.

This is the whole engine. A plan that's behind gets *filled*; a plan that's ahead gets *eased or protected*. It's
per-plan and self-correcting, so you never hand-tune "send X% to lane Y" — the clock and the gauge decide.

See it any time:

```bash
spendguard lanes --economics
```

Each plan prints its binding window, tokens left, **and its pace line** (`pace +0.31: BEHIND …` /
`pace -0.85: AHEAD … → SHED (protected)`).

---

## What you declare (all in `~/.spendguard/config.json`, via `spendguard config set`)

### 1. Your plans and fees — `subscription.plans` / `subscription.lane_plans`

So the receipt names each plan and the economics can price a window. Example:

```bash
spendguard config set subscription.plans '[{"name":"Claude Max","usd":200},{"name":"ChatGPT Pro","usd":200},{"name":"Gemini","usd":50},{"name":"GLM Coding Max","usd":118}]'
```

`subscription.lane_plans` maps a **lane** to its monthly fee for the exact per-lane split (otherwise the total is
divided evenly, marked with a `*`):

```bash
spendguard config set subscription.lane_plans '{"claude-code":200,"codex":200,"gemini":50,"zai-coding":118}'
```

### 2. Your value policy per plan — `subscription.pace`

For each lane, `policy` is one of:

| policy | behind pace | ahead of pace | use it when |
|---|---|---|---|
| `maximize` (default) | boosted — fill it | eases off in ranking | a plan you simply want to use up every window |
| `protect` (a.k.a. `conservative`) | still absorbs work | **shed entirely** — discretionary work goes elsewhere | a plan whose allowance you're *reserving* for work only it can do |

`protect` is for the plan you don't want to run dry on background work — e.g. a **Claude Max weekly reserved for
interactive coding**: when it's ahead of pace (burning fast), spendguard stops sending it fungible/background calls
so its remaining allowance lasts to reset, while the background work flows to whichever plan is behind (or metered).

```bash
# protect the Claude weekly; leave the rest on the default 'maximize'
spendguard config set subscription.pace '{"claude-code":{"policy":"protect"}}'
```

A protected plan that is **behind** pace still absorbs work — protection only *sheds when ahead*. It never starves a
plan that has budget to spend.

### 3. Routing groups — `advisor.tiers`

So a fungible caller asks for a **group you declared**, not a pinned model, and the router water-fills that group
across whichever plan/credit is best right now:

```bash
spendguard config set advisor.tiers '{"cheap":["glm-5.3","gpt-5.6-luna","gemini-flash-lite-latest"],"strong":["gpt-5.6-sol","claude-opus-4-8","gemini-3.7-flash"]}'
```

**You** decide which of your own models go in a group — it's your declaration that they're interchangeable for that
purpose (the capability judgement is yours, made once, by whoever knows the models; spendguard asserts nothing about
what a model can do and ships no built-in groups). A **lane serves a group** when the model you configured for it
(`advisor.lane_models[lane]`) is in that group's list. So if your Codex lane runs `gpt-5.6-sol` and `strong`
includes `gpt-5.6-sol`, a `strong` request can land on Codex at $0. If no $0 lane serves the group, it falls through
to the cheapest metered model in the list. Unset `advisor.tiers` → no group routing, and callers keep their normal
path. When a new model appears, add it to the group — the same one-line config edit as any other model list.

### 4. How hard to chase pace — `advisor.lane_pace_weight`

Default `2.0`. `0` ignores pace (rank on headroom alone); higher chases 100%-of-window utilisation harder.

---

## How a call gets routed (the value router)

`route_utility.rank_for_tier(tier, est_in, est_out)` returns targets best-first:

1. **Available $0 subscription lanes** whose model is in the tier — **pace-ordered** (behind-pace first), with any
   **protected, ahead-of-pace** plan excluded and a reason given.
2. then the **cheapest metered tier-model that still has prepay**,
3. then metered.

The caller routes to the first `available` target. That's it — no pinned model, no per-caller lane logic. Declare a
tier; the economics pick the plan. The same ranking drives the main dispatch path (`lane_balance.idle_lanes`), so
existing fungible work becomes pace-aware automatically with no code change on the caller side.

Nothing is ever silently dropped: a cooling or protected lane still appears in the ranking with `available=False`
and a `why`, so "why didn't it use plan X?" is always answerable from the output.

---

## Worked example

Plans: Claude Max ($200, weekly tokens), ChatGPT Pro/Codex ($200), Gemini ($50, weekly), GLM Coding ($118, N prompts/7d).
Mid-week, the Claude weekly is at 15% left and **ahead of pace (−0.85)** — it will run dry before reset. Gemini is
exhausted; the GLM plan has hit its self-use reserve; Codex has capacity.

```bash
spendguard config set subscription.pace '{"claude-code":{"policy":"protect"}}'
```

Now a `strong` fungible call ranks: **Codex ($0) → [claude shed: "protected + ahead of pace"] → [gemini: cooling]
→ metered**. Background work flows to Codex, and the Claude weekly's last 15% is preserved for interactive coding
that can't run anywhere else — used to ~100% by reset, not over. When the week rolls over and Claude is *behind*
pace again, it starts absorbing fungible work once more, automatically.

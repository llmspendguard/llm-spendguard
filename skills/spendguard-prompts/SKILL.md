---
name: spendguard-prompts
description: Run the prompt-efficiency loop — lint the call corpus for prompt waste (repeated boilerplate, context stuffing, truncation, cheaper-model candidates), then walk the top finding through batch-1 and a graded A/B experiment. Use when the user asks "are our prompts efficient", "how do we cut prompt cost", "audit our prompts", or wants to test a cheaper model/prompt without losing quality. Lint is zero model spend; experiments are caged + estimate-first.
---

# spendguard-prompts — the prompt lab

The loop (full doc: docs/PROMPT-EFFICIENCY.md): **lint → batch-1 → A/B → promote-and-keep**.
Quality is decided by the equivalence ladder — never by the price tag.

## 1. Lint (free)
```
spendguard prompts            # findings ranked by measured $ at stake; --intent X to narrow; --json for parsing
```
Present the findings as a table (kind · intent · $ at stake · the fix). If empty: check `calls.enabled`
(+ `calls.store_prompts` for the boilerplate check) — no corpus, no lens; tell the user what to enable.

## 2. Walk the TOP finding with the user
- **boilerplate** → propose moving the shared prefix to a cached system prompt / packed-batch template.
- **context_spread** → inspect 2–3 of the largest calls (`calls` table `in_tok` outliers); what's stuffed?
- **truncation** → apply the recommended `max_tokens ≈ p99×1.5`, note `spendguard maxtokens <sig>` for batch sigs.
- **model_mix** → the A/B below decides, not the price.

## 3. Batch-1, then the graded A/B (caged by caps.meta, ESTIMATE-first)
Any changed prompt/path is a fresh test — run ONE item and eyeball it before scaling. Then:
```
spendguard experiment '<intent>' --n 20                       # estimate only (default)
spendguard experiment '<intent>' --models gpt-5-nano --n 20 --run
spendguard experiment '<intent>' --semantic rubric --run      # prose outputs: caged LLM judge
spendguard experiment '<intent>' --semantic custom:mypkg.judge.score --run   # user's own (ref,out)->0..1
```
Show the estimate and get the user's OK before any `--run`. Read the verdict from the equivalence
tier + score, and say plainly whether the cheaper variant held quality.

## 4. Promote and keep
```
spendguard promote --intent '<intent>' …    # winner runs on a real chunk; output KEPT; insight recorded
```
Close by summarizing: what changed, measured $/call before→after, the equivalence tier that proved it,
and that the insight is now recorded (it will be re-validated as the corpus grows).

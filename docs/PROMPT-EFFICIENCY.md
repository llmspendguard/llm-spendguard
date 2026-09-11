# Prompt efficiency — the measured loop

Are your prompts efficient? Don't guess — the corpus you already have answers it. This is the loop that
turns call history into cheaper, equally-good prompts, with every step measured and every change gated.

## 0. Prerequisite: the corpus
Enable the opt-in call-context store — `calls.enabled` (+ `calls.store_prompts` for the snippet-based
checks; snippets are local-only and privacy-scoped). Every gated call then records intent, model, tokens,
cost, finish reason, and quality signals. All analysis below is **zero LLM spend**.

## 1. LINT — `spendguard prompts`
Mines the corpus per intent (≥5 calls; the law of small numbers is respected) and ranks findings by
measured $ at stake, each with its exact next command:

| Finding | What it caught | The move |
|---|---|---|
| `boilerplate` | a long shared prefix re-sent on every call (≥60 chars and ≥50% of the median prompt) | move it to a cached system prompt / packed-batch template — cached input is ~10× cheaper |
| `context_spread` | input p95 ≥ 3× p50 (and ≥500 tok apart) — the big calls are stuffing context | trim retrieval on the top decile, verify equivalence |
| `truncation` | `finish=length` observed — truncated output wastes the whole call | set max_tokens ≈ p99×1.5 (batch sigs: `spendguard maxtokens <sig>`) |
| `model_mix` | the same intent already runs ≥2× cheaper on another model | a measured cascade candidate — let the ladder decide |
| `batch_savings` — **opt-in `--judge-batchable`** (spends a small meta-caged call/candidate) | many (≥20) small **realtime** calls of one shape, **agentically ruled PACKABLE** (an LLM decides job-items vs latency-sensitive interactive turns — never a threshold) | pack many per call + use the Batch API; saving priced from `batch_cost` vs the metered $ actually paid (a floor; $0 lane-served calls have nothing to save) |

> `batch_savings` is the one finding that is not $0: whether many small realtime calls are *packable* (a batch job) or must stay realtime (interactive) is a **meaning** judgement, so it is decided by an LLM under `--judge-batchable`, never by a threshold. Every candidate's outcome (packable / not / unjudged) is tallied so a skip is never silent.

`--intent X` narrows, `--json` for machines. Prices come from `pricing.py` only.

## 2. Batch-1 of the same shape
Any changed prompt/path is a FRESH test: run ONE item end-to-end and eyeball it before scaling.
(The gate enforces this heuristic for large batches — an untested shape gets warned/refused.)

## 3. A/B — `spendguard experiment '<intent>' --n 20 [--models …] [--semantic …]`
Graduated comparison on real samples from your corpus, caged under `caps.meta`, estimate-first.
Equivalence is a **graded ladder**, not vibes: exact → JSON-scalar → text, with opt-in semantic tiers.

**Pluggable judges** — bring your own equivalence check:
```
--semantic embed                      # embedding cosine (caged)
--semantic rubric                     # LLM judge (caged)
--semantic custom:mypkg.judges.score  # YOUR callable (ref, out) -> 0..1
```
The custom callable can wrap anything — promptfoo assertion sets, JSON-schema validators, a domain
checker. Its score rides the same promote/keep decision as the built-in tiers.

## 4. Promote and keep — `spendguard promote`
The winning variant runs on a real chunk and its output is KEPT (workload, not throwaway), so the test
paid for itself. The result is recorded as an insight (condition → action → mechanism, confidence-scored)
and re-validated as the corpus grows — a refuted "win" gets demoted automatically.

**The invariant across the whole loop: quality is decided by the equivalence ladder and the recorded
insight lifecycle — never by the price tag.** A cheap prompt that fails equivalence is not a win.

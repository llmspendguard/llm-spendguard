# `docs/` — design & methodology

Deeper write-ups behind the code. Start with the root [README](../README.md) for usage and the full
command reference; these explain the *why*. Project home: https://llmspendguard.com.

| doc | what it covers |
|---|---|
| [ARCHITECTURE.md](ARCHITECTURE.md) | The system, with diagrams: the gate chokepoint, pricing resolution, the learning loop, the meta cage, data/isolation, and the honest known-limitations. **Start here for the design.** |
| [USING-WITH-CLAUDE.md](USING-WITH-CLAUDE.md) | Make **every** AI-assistant conversation wire in spendguard: `install-rule` (the standing `CLAUDE.md` rule) + how it pairs with the run-time enforcement layers + slash-commands. |
| [learning-advisor.md](learning-advisor.md) | The #7 learning advisor + temporal learning graph: *recommend **considering** history, not parroting it*. Data model (calls corpus → insights → graph), the Layer-1 deterministic / Layer-2 caged-LLM split, the **meta-budget cage** (the governor governs its own LLM use), conversation/script mining, and confidence-scored living insights. Status: implemented. |

### Methodology in one screen
- **Correct prices, always** — one canonical table, layered + cross-checked (LiteLLM + OpenRouter), never hardcoded; an `audit` enforces it. (The original $149.76 bug was a hardcoded price.)
- **Estimate before spend** — every paid path projects cost first; the gate hard-stops over caps (but asks, if interactive).
- **Cost-per-GOOD-result** — a cheap call that fails quality is 100% waste, so the metric is `$/good`, and any model/format downgrade is **quality-gated** (proven by `experiment`, not assumed).
- **Measure, don't assume** — recurring lesson: empirically verify (prompt-caching opportunity, model fitness, reasoning settings) instead of guessing.
- **The governor is caged** — the advisor's own LLM use has a separate `caps.meta` budget, is tagged `spendguard:*`, and is excluded from the corpus it analyzes, so it can't overspend or pollute its own learning.
- **Living, validated learnings** — insights are conditional rules with a confidence + lifecycle, re-validated as data grows; the cheapest-config-that-held-quality feeds back into the next plan.
- **Self-contained & non-blocking** — zero required deps, fail-open, state isolated under `$SPENDGUARD_HOME`; observability is exported (OTel), not another dashboard.

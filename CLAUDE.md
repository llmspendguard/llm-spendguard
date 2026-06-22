# llm-spendguard — agent operating doctrine

## #1 lens: AGENTIC AT HEART
llm-spendguard is **agentic at heart**, and that is the lens for evaluating EVERY development decision here.
Before writing or changing anything, ask: *is this the agentic choice?*

- **Decisions about MEANING are made by an LLM, never by regex/keywords.** "What project/org is this work?",
  "what was this spend for?", "is this output good?", "what changed?" → an LLM reads the context and decides.
  Regex is allowed ONLY for trivial mechanical extraction (finding a `batch_…` id, splitting a date) — never to
  DECIDE meaning.
- **NEVER de-agentic-ify to save money.** Cost is controlled by the spendguard RAILS — the gate, estimate-first,
  Batch-API packing, caching, a cheaper model, and recording results so we never re-pay — NOT by swapping the LLM
  for a keyword hack. A $0 attribution that is wrong is worth less than nothing.
- **The core mission is correct ATTRIBUTION · DISCOVERY · CONTEXT.** If that is wrong, nothing downstream
  (dashboards, orgs, $ rollups, rebuilds) has any value. Cross-check every change against the core mission, and
  verify against GROUND TRUTH (provider totals + known repos) — never against fixtures rigged to pass.

See `docs/AGENTIC.md` for the architecture (the small+large convergence loop, the agentic boundary, the rails).

## How we stop re-learning (the anti-amnesia rule)
A lesson stated as prose is advisory and WILL be forgotten under focus. **A lesson is not learned until it is
ENFORCED by something that is not a human memory** — a test, a lint rule, a CI gate, or a runtime assertion.

- When a mistake is found, do TWO things: fix it, AND add the guard that makes it impossible to recur (a failing
  test / lint / assert). Example: the regex-attribution regression → `tests/test_segment_attribution.py` now fails
  if attribution ever stops being agentic or sends evidenced spend to "unattributed".
- If you are being reminded of the same thing twice, the fix is NOT "remember harder" — it is "where is the missing
  guard." Add it.
- Record the lesson in memory AND turn it into a guard. Prose + enforcement; prose alone does not count.

## Pre-change checklist (apply before any non-trivial change)
1. Is the decision about MEANING agentic (LLM), with regex only for trivial extraction?
2. Does it keep ATTRIBUTION/DISCOVERY/CONTEXT correct, cross-checked vs GROUND TRUTH (not rigged fixtures)?
3. Is there a small+large agentic LOOP that converges on correct (classify → cross-check vs truth → re-attribute the
   uncertain → repeat until it reconciles)?
4. Is the agentic work RECORDED in the base sqlite so we never redo / re-pay for it?
5. Is the lesson behind this change ENFORCED by a test/lint/assert so it cannot regress?
6. Is cost controlled by the rails (gate / estimate-first / batch / cache / cheap-model), never by de-agentic-ifying?

## Spend rules (inherited, non-negotiable)
All LLM code runs UNDER the gate (`import spendguard; spendguard.require()`; verify `spendguard doctor` =
ENFORCING). Estimate-first (a separate, zero-spend estimate) before any paid batch. Never hardcode prices (use
`pricing.py`). Prefer the Batch API for non-interactive work. Never cancel a running job as cost control —
completed requests still bill.

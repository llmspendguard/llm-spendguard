# llm-spendguard — agent operating doctrine

## #0 GROUND BEFORE YOU ACT (the anti-dysfunction rule)
The recurring failure here is acting from the LOCAL task instead of from the SYSTEM — narrowing scope, re-deriving,
and re-building things that already exist, which forces the same corrections again and again ("be agentic", "all
sources", "use what's already there"). Stop it at the source: **before changing any subsystem, GROUND first.**

Before writing code for a subsystem, do these IN ORDER and state the answers explicitly:
1. **What already implements this?** Find + read the existing code/pattern (e.g. `reconcile.Source`, `classify_items`,
   `batch_project_map`, `discover_agentic`). Extend it; do NOT rebuild. If you're describing a capability as missing,
   prove it's missing first.
2. **Is it agentic, and across ALL sources?** Meaning→LLM; and the change must hold for batch · realtime ·
   remote-compute, not one of them (see `docs/AGENTIC.md` §1b).
3. **Cross-check vs GROUND TRUTH, not rigged fixtures.** Σ attributed ≤ provider/account truth.
4. **What guard makes this un-regressable?** (test/lint/assert — see #anti-amnesia below.)

If you can't answer 1–4, you are not ready to write code. This rule exists because doctrine you don't consult at the
moment of acting is just wall-paper.

## #0b MATCH THE UNIT OF REVIEW TO THE UNIT OF THE DEFECT
A review can only find defects that FIT INSIDE the unit it looks at. Three waves of a four-vendor review,
~500 findings, and it missed 13 functions copy-pasted between two files, four writers of config.json, and a
producer/consumer pair writing and reading different tables — because `repo_review_panel.py` puts ONE FILE in
each reviewer's context. None of those was a diligence failure. The evidence was absent, and **no amount of
care recovers evidence that is not in the context window.** When something was missed, ask "was it even in the
room?" before asking "why wasn't it noticed?"

So a claim like "I reviewed the repo" is meaningless without the axis. There are five, and each is blind to
what the others find:

| axis | unit in context | finds | blind to |
|---|---|---|---|
| 1 FILE | one file | swallowed exception, wrong branch, unguarded index | anything whose other half is elsewhere |
| 2 CONCEPT | every implementation of one capability, in full | DRIFT — copies of one job that now disagree | a concept correctly duplicated |
| 3 SEAM | every writer + every reader of one resource | CONTRACT GAPS — each side correct, the gap wrong | anything that is nowhere |
| 4 INVARIANT | the whole repo vs one claim it makes | **ABSENCE** — a discipline present in NO file | nothing structural |
| 5 NAME | all definitions sharing a bare name | COLLISION (same name, different jobs) vs DUPLICATION vs PROTOCOL | — |

`scripts/probe/{capability_audit,review_capability_slice,review_axes}.py`, all on `repo_defs.py`.

**Axis 4 is the one that is skipped and matters most: it is the ONLY axis that can find something MISSING.**
"There is no backup before any mutation in this repo" is true, catastrophic, and appears in zero files, so
axes 1–3 can look forever and never see it. Before claiming a class of defect is closed, run axis 4 on it.

**Names are not evidence of concept.** A name and a docstring are the author's CLAIM about a function.
`bulkgate.record_estimate` and `calibrate.record_estimate` share a name and do different jobs; `share.scrub`
and `share._scrub_text` share nothing and do the same job. Cluster from BODIES. Names are for IDENTITY only —
and identity is SCOPED: `module.Class.method`, never `module.method`. A flat key collided 11 times here and
made a reviewer read one body while reporting on another.

**Function names should be unique across a repo.** Currently 81 bare names cover 265 definitions (23%).
Same-name/different-job is the dangerous case: a grep, an import, or a re-export silently picks a side.
Same-name/same-interface (a protocol implemented by five providers) is correct and must not be "fixed" —
which is why sorting a collision is a judgement for a model, never a rule about names.

**A regex that decides what the evidence IS makes the regex the decider.** The seam axis first grounded
itself with `INSERT INTO (\w+)` / `FROM (\w+)`. A table name built with an f-string, or a write performed by
a helper, then reads as *nothing* — and a missed writer does not look like a miss, it looks like a clean
one-sided seam. If a regex feeds an agentic judgement, the judgement inherits its errors silently. Ground
agentically or do not claim the ground truth.

**"Cannot tell" is not "clean."** The invariant axis was handed an inventory of names and asked a question
about function bodies; the model correctly said it could not answer, and the tool printed ABSENT. The
checker violated the invariant it was checking. Every axis must report UNREVIEWED / INSUFFICIENT separately
from a clean result, and a coverage denominator with it.


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

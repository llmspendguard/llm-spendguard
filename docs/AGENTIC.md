# AGENTIC.md — the architecture doctrine for llm-spendguard

llm-spendguard is **agentic at heart**. This document states what that MEANS in code, so the `CLAUDE.md` doctrine
is concrete and enforceable. It is the architecture indication: read it before designing any feature that touches
attribution, discovery, or context.

## 1. The agentic boundary — meaning vs mechanics
- **Meaning → LLM.** Attribution (work → project · org · context), discovery (what work happened and why), quality
  judging, classification, synthesis. A human would use *understanding* to decide these, so we use a model.
- **Mechanics → deterministic code.** Reading files, finding a `batch_…` id string, summing tokens, pricing from a
  table, hashing, upserting rows. No judgement → no LLM.
- **The anti-pattern that broke us:** using regex keywords to DECIDE a project — `re.search("embedding|corpus", text)`
  is a *meaning* decision dressed as mechanics. It silently misclassified real work into "unattributed" and no test
  caught it. Banned. Regex may find a *string*; it may never decide *what work this was*.

## 2. Attribution is agentic, per-subconversation, prior-confirmed
- A transcript (one session) can span MULTIPLE projects → attribution is at the **subconversation** level, not the
  whole session.
- The repo/cwd is a **PRIOR, not the answer**: the LLM defaults to the repo's project but CONFIRMS or OVERRIDES per
  content (a session in `lmm/` may contain manga2anime work).
- Spend (a batch, a realtime span) attributes to the **segment that produced it** → that segment's classified
  project/org. Magnitude comes from provider TRUTH; the LLM only decides WHERE it lands. Σ attributed ≤ provider truth,
  always.

## 3. The convergence loop (small + large) — converge on CORRECT, not "ran once"
Agentic work is a LOOP that self-corrects until a measurable correctness criterion is met.
- **Small loop (per segment):** classify → confidence. If confidence is low, or the prior is coarse (a parent dir
  like `Documents/`), re-prompt with MORE context (fuller text, neighbouring turns) → a higher-confidence answer.
  Verify the project exists in the taxonomy; if it is new, confirm it.
- **Large loop (corpus):** attribute all spend-bearing segments → **cross-check vs GROUND TRUTH** (Σ per project ≤
  provider truth; flag the coarse / low-confidence / "no-conversation" buckets) → re-attribute ONLY the flagged ones
  agentically → repeat until no coarse bucket exceeds a $ threshold and the totals reconcile.
- The criterion is measurable, so the loop terminates on *correct*.

## 4. Record once, never re-pay — the base sqlite
Every agentic decision is persisted in the base sqlite table `seg_attribution`
(`seg_id, content_hash, sid, cwd, prompt, project, org, team, confidence, source(prior|llm|human), model, ts,
batch_ids`). Therefore:
- a re-run reuses prior decisions for FREE — only NEW / low-confidence / stale segments are re-classified;
- **human overrides** (`source=human`) are durable and beat the LLM;
- the loop's "what still needs the agent" query is simply `WHERE source!='llm' OR confidence < τ`.

## 5. Cost is controlled by RAILS, never by going dumb
The gate (every call caged + estimated), estimate-first (a zero-spend projection), Batch-API packing, response
caching, the cheapest model that passes, and record-once. We spend on intelligence deliberately; we never trade
correctness for $0.

## 6. The enforcement contract (anti-amnesia)
Every doctrine point has a guard so it cannot silently regress — this is how we stop re-learning:
- *no regex attribution* → `tests/test_segment_attribution.py` (the regex fn is deleted; evidenced spend can never
  fall to "unattributed"; the path stays agentic).
- *Σ ≤ provider truth* → the reconcile residual surfaced + asserted.
- **Every new lesson adds its own guard.** A lesson without a guard is not done.

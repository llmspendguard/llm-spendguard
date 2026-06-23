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

## 1b. ONE agentic process across ALL spend sources — batch · realtime · remote-compute
There are THREE spend sources and they all go through the SAME agentic attributor. No source gets a non-agentic
shortcut (that is how realtime ended up on a fallback and GPU on a label-map while only batch was agentic — the
exact mistake to never repeat). Each source is a `reconcile.Source` (truth_total = magnitude; captured; attribute_gap),
and `attribute_gap` is the SAME segment classifier for every source:

| source | magnitude (truth) | link to a segment (the SAME agentic classifier attributes it) |
|---|---|---|
| **batch** | OpenAI/Anthropic batch ledgers | a `batch_…` id appears in a transcript → segment |
| **realtime** | gate realtime log + **reconstructed-from-conversations** | the segment active at the call; for REMOTE realtime (LLM calls run on vast.ai boxes) the VOLUME is reconstructed from the fleet-orchestration conversation |
| **remote compute** (vast.ai GPU) | vast.ai billing | the conversation that launched/configured the instance → project · org · **user** |

Consequence for the segmenter: it must capture not only batch-ids but also vast.ai instance refs / remote-run
evidence, so realtime and GPU units link to their segment too. Magnitude always comes from the source's truth; the
agentic classifier only decides WHERE it lands; Σ attributed ≤ truth; the convergence loop reconciles all three.

## 1c. REALTIME accounting is CORE and the standard process needs NO admin key (settled — stop re-litigating)
This has been re-derived too many times. The standard, ongoing realtime process is **admin-key-free**; the admin key
is a **DEV-only cross-check**, never a runtime dependency. Concretely:

- **FORWARD (the standard capture):** every realtime call runs UNDER THE GATE, which intercepts the SDK (openai
  `chat.completions`, anthropic `messages`; `RT_INTERCEPTORS`) and records the ACTUAL input/output tokens at call
  time (`gate._rt_record` → `realtime_log.jsonl` + the ledger). Exact, inline, no admin key. This is why "run it under
  the gate" is the mandate — ungated realtime is the only way spend escapes (that was the $1.4k historical gap).
- **HISTORICAL (one-time):** pre-gate / ungated realtime is NOT chat-reconstructable, so it is recovered ONCE via the
  admin **usage** oracle (`realtime_oracle.by_project_day`, tokens×pricing, timing-matched per project), **recorded**
  into the ledger by `reconcile_realtime` under `SPENDGUARD_ADMIN_ORACLE` (dev). Once recorded it persists — the
  keyless client pushes it and the daily no-key sync PRESERVES it (record once).
- **The admin key's ONLY job** is dev-time cross-checking (does the gate-captured realtime match the provider usage
  truth?) + that one-time backfill. The shipped client / daily scheduler reads NO admin key: every admin path is
  gated behind `SPENDGUARD_ADMIN_ORACLE` (off by default).

ENFORCED, not just stated: `test_gate` (gate captures realtime inline, no key), `test_reconcile_core` (flag OFF →
oracle not consulted, realtime still reconciles via the gate — the CLIENT-needs-no-admin-key invariant),
`test_ledger_sync` (oracle records per-project; the no-key path preserves the markers — record once). If realtime ever
regresses to needing an admin key for normal running, those fail.

## 2. Attribution is agentic, per-subconversation, prior-confirmed
- A transcript (one session) can span MULTIPLE projects → attribution is at the **subconversation** level, not the
  whole session.
- The repo/cwd is a **PRIOR, not the answer**: the LLM defaults to the repo's project but CONFIRMS or OVERRIDES per
  content (a session in `lmm/` may contain manga2anime work).
- Spend (a batch, a realtime span) attributes to the **segment that produced it** → that segment's classified
  project/org. Magnitude comes from provider TRUTH; the LLM only decides WHERE it lands. Σ attributed ≤ provider truth,
  always.
- **A KEY does not imply an org. Given a SET of org keys (one machine, several orgs/keys), the org↔spend resolution is
  AGENTIC — it resolves to what MAKES SENSE from the evidence, not from which key happened to push.** A shared provider
  key serves multiple orgs; the same machine holds several org keys. So: the AGENTIC attribution of the segment/box
  decides the org (per §1/§2), and the key is just the push credential that must MATCH that resolved org — never the
  other way round. The push SCOPE follows from the resolved org (the taxonomy's org→projects), not a hand-kept list —
  that drift is exactly what dropped concept-model/medical-taxonomy and leaked Ensight's llmseg into Healiom. Corollary:
  a connection that can't resolve its org to a scope fails CLOSED (push nothing) rather than guessing or pushing all.

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

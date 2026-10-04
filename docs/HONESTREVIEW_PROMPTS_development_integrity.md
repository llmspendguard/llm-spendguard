# honestreview prompts — development-integrity doctrines to STOP "built-but-not-wired / shallow-test / hollow-green"

Paste each block, in order, into a honestreview session (`cd ~/Documents/claude/honestreview`). Each creates ONE
doctrine (or capability) that turns a repeated development failure into an **authorship-time block** — the only thing
that makes "done is done" structural instead of a promise. They are written to honestreview's own conventions:
decisions are AGENTIC (an LLM judges meaning; regex only parses fixed shapes), every doctrine reports
UNREVIEWED/INSUFFICIENT separately from clean with a coverage denominator ("cannot tell" ≠ "clean"), and every new
doctrine ships with a guard test that it FIRES on a deliberate violation and stays silent on a clean case.

## Why these exist (the failures they each prevent, measured on llm-spendguard 2026-10-04)
- A capability was *built but never wired* (`should_batch_fan`/`chunk_for_batch` had no live caller) and reported as
  "fixed." → **Prompt 1**.
- A test was *named for an end-to-end behavior but only called internal functions* ("storm→batch→responses" asserted
  by calling `should_batch_fan()` directly, never submitting N and getting N back). → **Prompt 2**.
- There was *no recorded list of required tests*, so "I'll test X" was never tracked or coverage-asserted; promises
  evaporated. → **Prompt 3** (the manifest — the answer to "is that test added to the list of tests?").
- A *live test passed while recording zero real effect* (0 ledger rows, $0, served from cache/no-op) — a hollow
  green. → **Prompt 4**.
- The test suite's *intent was never checked by anyone but the author* — single-reviewer blindness. → **Prompt 5**.
- Nothing *composed* these into a single "done" gate, so a capability could slip through on any one axis. → **Prompt 6**.

---

## PROMPT 1 — doctrine `forward_wiring` (built-but-not-wired)

> Create an agentic honestreview doctrine `forward_wiring`. For each NEW or CHANGED public capability in the staged
> diff — a function/method/class that is not underscore-private, a CLI subcommand, an MCP tool, or a config knob —
> judge (LLM, from the code, not a regex) whether it has a LIVE CALLER reachable from a real execution entry point
> (an API surface, a CLI command, a hook, a scheduled job, or another live capability) — NOT only a test and NOT only
> another unreferenced definition. Flag a capability that is defined, exported, or documented but has NO live consumer
> as **built-but-not-wired** (the "capability built and then not wired in" from the global NO-SHORTCUTS rule).
> Ground the call graph AGENTICALLY (an f-string-built dispatch, a registry lookup, or a getattr wiring must count as
> a caller — a regex over imports would miss them and declare a wired capability orphaned). Report UNREVIEWED
> separately from clean with a coverage denominator. Exempt a capability explicitly annotated as a public API meant
> for external callers (a declared, not assumed, exemption). Ship a guard test: an intentionally-orphaned function is
> flagged; the same function given a live caller is not. Wire it into the axis set + the commit dispatch hook.

## PROMPT 2 — doctrine `test_exercises_claim`

> Create an agentic doctrine `test_exercises_claim`. For each NEW or CHANGED test, read its name + docstring to infer
> the CLAIM it makes (what behavior it purports to prove), then read its body and judge (LLM) whether it actually
> EXERCISES that claim. Flag as **does-not-prove-its-claim** a test whose name/docstring asserts an END-TO-END or
> behavioral outcome (e.g. "submit N → N responses", "storm → auto-batch → results returned", "X is paced") but whose
> body only calls internal primitives or decision-functions, asserts their return values, or stubs the behavior —
> never driving the real entry point to produce the real output the claim is about. The test of the test: "if the
> wired behavior were entirely absent but the primitives still returned these values, would this test still pass?" If
> yes, it does not prove its claim. Distinguish a legitimately-scoped UNIT test (whose name claims only the unit) from
> an end-to-end claim tested shallowly. Report UNREVIEWED vs clean + coverage. Guard test: a shallow test with an
> end-to-end name is flagged; a genuine end-to-end test, and an honestly-named unit test, are not. Add to the axes +
> commit hook.

## PROMPT 3 — capability `required_tests` manifest + doctrine `coverage_is_asserted` (the recorded list)

> Create a durable REQUIRED-TESTS MANIFEST and the doctrine that enforces it — this is the answer to "is that test
> added to the list of tests?". The manifest (a versioned file in the repo, e.g. `docs/REQUIRED_TESTS.json`) maps each
> declared CAPABILITY/REQUIREMENT (keyed by a stable id) to: the test(s) that prove it, the entry point they drive,
> and whether live evidence is required. Create the doctrine `coverage_is_asserted`: on commit it (a) agentically
> detects a new/changed capability or a stated requirement (in code, docstrings, a PLAN doc, or a PR description) that
> has NO manifest entry → flags **unlisted-capability** (every capability must declare its required tests, so a
> promise to "test X later" is recorded, not evaporated); (b) for each manifest entry, asserts the named test EXISTS,
> is wired to the stated entry point (reuse `forward_wiring`), and PASSES — flagging a missing, orphaned, or
> no-op/mocked-pass test as **unmet-required-test**; (c) emits a coverage line: `K of N declared capabilities have a
> passing, claim-exercising required test` — so "is everything actually tested?" is answerable with a number, and a
> drop in coverage blocks. Seed the manifest from the cross-provider test-set panel (see the companion
> `scripts/probe/panel_full_test_set.py`). Guard test: a capability with no manifest entry is flagged; one whose
> required test is a no-op is flagged; a fully-covered one is clean. Wire into the deploy gate.

## PROMPT 4 — doctrine `live_evidence_or_not_green` (no hollow green)

> Create a doctrine `live_evidence_or_not_green`. For a test or validation that CLAIMS a real-world effect — calls
> made, tokens/cost incurred, responses returned, rows written — require MEASURED EVIDENCE that the effect happened
> (e.g. ledger rows for the run's chain, billed > 0 where spend is claimed, non-empty real outputs, a DB delta), and
> flag as **hollow-green** a run that PASSES while recording zero real effect (served from cache, a no-op, a stubbed
> path, or a gate that short-circuited execution). The judge compares the claim ("this validates the metered path")
> to the observable evidence ("0 recorded calls, $0 billed") and fails the mismatch. This is the exact failure where a
> live 429-validation printed PASS on 0 executed calls. For an offline test that legitimately mocks, require it to be
> NAMED as offline/mocked (so its green is not read as live proof). Guard test: a validation asserting billed>0 that
> records $0 is flagged; one with real ledger evidence is clean. Add to the axes.

## PROMPT 5 — capability `panel_intent_review` (multi-provider "does the suite prove the intent?")

> Create a `panel_intent_review` capability: given a capability/requirement and its test suite, ask a CROSS-VENDOR LLM
> panel (anthropic + openai + gemini + moonshot + zai — route $0 via the subscription lanes, metered only if a lane is
> down) the single question "does this test suite ACTUALLY and EXHAUSTIVELY prove this intent, and what tests are
> missing?" Record each model's verdict + the UNION of missing-test findings into the `required_tests` manifest as new
> required entries (so the panel's enumeration becomes tracked coverage, not a one-off chat). A capability is not
> "done" until the panel's missing-test set is empty or each item is explicitly waived with a recorded reason. This
> removes single-reviewer (single-model) blindness — the author's model is not the sole judge of sufficiency. Make it
> runnable on demand for a named target and surfaced in the deploy gate's "done" report.

## PROMPT 6 — doctrine `done_gate` (compose the above into one verifiable definition of done)

> Create a `done_gate` doctrine/report that composes 1–5 into a single, mechanical definition of DONE for a capability:
> it is done ONLY when it is (1) WIRED (`forward_wiring` clean), (2) its tests EXERCISE their claims
> (`test_exercises_claim` clean), (3) present in the `required_tests` manifest with all required tests PASSING
> (`coverage_is_asserted` clean), (4) backed by LIVE EVIDENCE where a real effect is claimed
> (`live_evidence_or_not_green` clean), and (5) the cross-vendor `panel_intent_review` has no outstanding missing
> tests. Output a per-capability DONE / NOT-DONE with the specific failing axis named, so nobody — human or model —
> can declare "done" while any axis is red. Wire it into `honestreview ci` and the spendguard deploy gate, and have it
> print the coverage denominator so the state is always visible.

## PROMPT 7 — Stop-hook `claim_auditor` (POST-TURN: a claim without evidence, or a promise without a tracked action, is caught)

> This is the enforcement for the layer the commit-time doctrines CANNOT see: what the assistant SAYS. Create a
> POST-TURN hook (a Stop hook — it runs when the assistant finishes a turn) `claim_auditor` that reads the turn's own
> assistant output and agentically (LLM, meaning not keyword-grep) extracts two kinds of statement:
> - **DONE-CLAIMS** — "done / fixed / verified / it works / wired / tested / green / proven".
> - **COMMITMENTS** — "I'll test X / next I'll wire Y / I'll add a guard / I'll validate Z".
>
> For each DONE-CLAIM, cross-check GROUND TRUTH: is the claimed capability in the `required_tests` manifest with a
> test that PASSES, EXERCISES ITS CLAIM (Prompt 2), and is WIRED (Prompt 1) — plus LIVE EVIDENCE (Prompt 4) where a
> real effect was claimed? If not → emit a loud, blocking integrity finding: **"claimed DONE without backing
> evidence: <claim>"** — the exact "yes-I-did-but-no-I-didn't" pattern, caught at the turn boundary instead of three
> turns later when the user notices.
>
> For each COMMITMENT, ensure it is recorded as an OPEN item in the `required_tests` manifest / a commitments ledger
> (auto-add if missing), so a promise to "test X later" CANNOT evaporate between turns — a later turn or the deploy
> gate must satisfy it or explicitly waive it with a reason. Surface the open set each turn so it stays visible.
>
> So yes — it is a post-turn analysis that (a) checks what was claimed/promised, (b) forces unbacked done-claims into
> view as integrity violations, and (c) forces commitments into tracked, must-close items. Agentic extraction; ship a
> guard: a turn asserting "done, verified" with no manifest/test/ledger backing is flagged; a turn with real backing
> is clean; an "I'll test X" with no manifest entry auto-creates an open item. Install it as a Stop hook alongside the
> PostToolUse doctrine dispatch.

## PROMPT 8 — PRE-CODE gate `system_model_before_code` (think at the architecture level FIRST — the missing layer)

> This is the EARLIEST layer — it fires BEFORE implementation, not at commit, and it is the direct enforcement for
> the failure the other seven cannot prevent: coding from the LOCAL task instead of the SYSTEM (llm-spendguard
> CLAUDE.md #0 already names this exact failure — it recurs precisely because it was advisory, never gated). Create a
> gate `system_model_before_code`: for any non-trivial implement / fix / change task, REQUIRE a written SYSTEM MODEL
> artifact, produced and judged BEFORE code is written, containing:
> - the full **PIPELINE** the change sits in, every stage named end-to-end (for the 429 work: submit → coalesce →
>   admit → per-vendor bucket → egress → batch → unbundle → future);
> - the **INVARIANTS** that must hold at EVERY scale (conservation N-in == N-out; ONE chokepoint — nothing bypasses
>   admission; no double-spend; demux/order correctness);
> - the **FIRST-PRINCIPLES behavior of each mechanism the design TRUSTS** — the agent must OPEN each one, not assume it
>   (e.g. "this rate limiter is a token bucket — does it start full? then it does NOT pace the first burst"; "a
>   concurrency bound is NOT a rate bound when latency is low"). This single item is what would have surfaced the
>   burst-allowance root cause from reasoning, before measuring 931 thrashed 429s.
> - the **FAILURE MODES at 1 / at 1000s / at sustained sub-window / at concurrent-cross-process**, each enumerated WITH
>   the design's answer;
> - THEN the implementation plan as an INVARIANT → TEST MAP: every invariant is a row mapped to the test that proves
>   it, and each row is EITHER (a) proven by a passing, claim-exercising test, OR (b) explicitly DEFERRED with a
>   `required_tests` manifest entry (Prompt 3) AND a one-line safety argument for why deferring it does not silently
>   break the contract now. A deferred invariant with no manifest entry or no safety note is flagged **unjustified-
>   deferral** — this closes the hole where a ⏳ row quietly drops an invariant.
>
> The judge (LLM, meaning not keyword) flags an implementation task that starts coding with NO system-model artifact,
> or whose artifact omits the pipeline / invariants / mechanism-first-principles / scale-failure-modes, as
> **coded-before-understood** — a blocking finding. Report UNREVIEWED vs clean + coverage. Ship a guard: a task that
> jumps straight to code is flagged; one with a substantive system model is clean. **Route the system-model pass to
> the STRONGEST available reasoning model** (opus-5 / sol / fable) — architecture depth is high-stakes and low-token,
> exactly where best-value routing should spend UP, while mechanical implementation runs cheaper. This converts latent
> systems knowledge into APPLIED reasoning at the one moment it matters (design time), and makes "think at the system
> level" structural instead of a hope — regardless of which model is driving.

## PROMPT 9 — capability `invariant_set_adversarial_review` (is the MAP complete, or did one mind miss a failure mode?)

> This is the completeness check Prompt 8 cannot perform on itself, and it was born from running Prompt 8 live: the
> system-model + invariant→test map in Prompt 8 is produced by ONE mind, so it inherits that mind's blind spots —
> exactly the single-reviewer blindness Prompt 5 removes for a test SUITE, but one layer earlier, on the INVARIANT SET
> that precedes the suite. Create a `panel_intent_review` sibling, `invariant_set_adversarial_review`: given a system
> model + its invariant→test map (the Prompt 8 artifact), ask a CROSS-VENDOR LLM panel (anthropic + openai + gemini +
> moonshot + zai — route $0 via the subscription lanes, metered only if a lane is down) three adversarial questions:
> 1. **COMPLETENESS** — "for a system of THIS class (here: a concurrent submission governor), what invariant or
>    failure mode is MISSING from this map?" Prompt the panel with the standard axes it must check against —
>    conservation, ordering/demux, rate safety, idempotency/resume, cross-process shared state, deadline/backpressure,
>    security, economics — and demand each MISSING invariant be named concretely, not "looks thorough".
> 2. **PROXY-TEST** — "for each row, does the named test actually PROVE that invariant, or a cheaper PROXY?" (the
>    test-of-the-test from Prompt 2, applied to the map before the code exists).
> 3. **DEFERRAL SAFETY** — "for each DEFERRED (⏳) row, is deferring it safe right now, or does the contract silently
>    break without it?" A deferral the panel judges unsafe becomes a blocker, not a ⏳.
>
> Record each model's verdict + the UNION of missing-invariant / proxy / unsafe-deferral findings as NEW rows in the
> map AND new entries in the `required_tests` manifest — so the panel's enumeration becomes tracked coverage, not a
> one-off chat. The design is not "ready to build" until the panel's missing-invariant set is empty or each item is
> explicitly waived with a recorded reason. Runnable on demand for a named design artifact; surfaced in the deploy
> gate's "done" report alongside `panel_intent_review`. Guard: a map missing an obvious invariant for its system class
> is flagged by the panel; a complete map passes clean.
>
> WHY SEPARATE FROM PROMPT 5: Prompt 5 reviews "does this test SUITE prove this intent" (code exists). Prompt 9 reviews
> "is this INVARIANT SET complete and safely deferred" (BEFORE code), catching the missing property while it is still
> cheap — a missing invariant found here costs a map row; found after shipping it costs an incident.

---

### Enforcement layers (so nothing slips through any one of them)
0. **Pre-code (design gate — Prompts 8 + 9):** BEFORE implementation, Prompt 8 requires a system model (pipeline +
   invariants + mechanism first-principles + scale failure-modes) as an invariant→test map, routed to the strongest
   model; Prompt 9 then has a cross-vendor panel adversarially check that MAP for completeness / proxy-tests / unsafe
   deferrals, so a missing invariant is caught while it still costs a map row, not an incident. This is the layer that
   stops coding-from-the-local-task — the deepest failure, and the one that produces wrong architecture nothing
   downstream can fix.
1. **Post-turn (Stop hook — Prompt 7):** catches CLAIMS and COMMITMENTS in chat — a "done" with no evidence is
   blocked; an "I'll test X" is auto-tracked. This is the layer that stops the dropped/faked promise.
2. **Authorship/commit-time (PostToolUse + precommit — Prompts 1, 2, 4):** catches the CODE versions in the diff —
   built-but-not-wired, a test that doesn't exercise its claim, a hollow-green validation.
3. **Deploy gate (`honestreview ci` + Prompts 3, 5, 6):** the composition — the `required_tests` manifest coverage
   must be green, the cross-vendor panel's missing-test set empty, and `done_gate` reports DONE/NOT-DONE per
   capability. Nothing merges "done" while any axis is red.

### Note on scope
These are development-integrity doctrines (process enforcement), complementary to honestreview's existing
code-quality doctrines (agentic_decisions, coding:python, enforce cross-file duplication, name-uniqueness,
raw_provenance, etc.). They target the layer those cannot see: *was the right thing built, wired, listed, tested, and
proven* — not just *is this file's code clean*. Build them smallest-first (1, then 2, then 3) since 3 and 6 depend on
1 and 2.

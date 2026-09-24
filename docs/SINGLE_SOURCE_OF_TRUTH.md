# Single-Source-of-Truth — one concern, one home (enforced)

**Status:** ADOPTED doctrine. **Filed:** 2026-09-24. **Why now:** the max-output-token ceiling was decided in ~10
places; any one could regress, and one did — a poison-prone learned fact truncated real answers repeatedly, costing
money and time. The bug was not the value; it was that *one concern lived in many places*. This doctrine makes that
structurally impossible for the concerns we register, and catches novel duplication agentically.

## The principle

**Every distinct CAPABILITY has exactly ONE home in the repo, and all callers use it.** A capability implemented in two
places is a latent divergence: the two drift, and a caller reasoning about one is wrong about the other. This is not a
style preference — it is the same standing as no-hardcoding and name-uniqueness.

## The abstraction rule (two → three)

- **1st implementation:** fine — it lives wherever it naturally belongs.
- **2nd implementation (a SMELL):** stop. Either call the first one, or — if both genuinely need it — ABSTRACT it now
  into a single home and register it (below). The doctrine FLAGS the second occurrence.
- **3rd implementation (FORBIDDEN):** hard block. It must be abstracted before it lands.
- **Adjudicated exception:** two implementations may be a genuine PROTOCOL (a uniform contract each module implements by
  design — e.g. every executor's `run_prompt(...)`). A protocol is recorded in the registry as such, not asserted in
  passing. Absence of a verdict is a violation, not a pass (same rule as `NAME_REGISTRY`).

## Placement convention (where the home goes)

- The home is the module that **OWNS the concern**: `pricing` owns per-model ceilings/prices; `adapters.output_budget`
  owns the output max_tokens actually sent; `models` owns per-model reasoning facts.
- A cross-cutting utility with **no natural owner** goes in a **named** shared module that says what it holds — never a
  vague `utils.py` / `helpers.py` / `misc.py` (that just relocates the sprawl).
- The home is a single function or a small cohesive class, referenced by every caller. Callers NEVER inline its logic.

## The methodology (identify → resolve), standardized

1. **Identify** — the `canonical_concerns` honestreview doctrine flags a diff that (a) re-implements a REGISTERED
   concern outside its home, or (b) duplicates, *by meaning*, a capability that already exists anywhere in the repo.
2. **Choose the home** — the owning module (placement convention above).
3. **Consolidate** — move the logic to the home; every caller calls it. No inline copies remain.
4. **Register** — add `{concern → home}` to `docs/CANONICAL_CONCERNS.json` with a one-line description + a `telltale`
   (what the concern's operations look like, so the doctrine can locate candidate sites).
5. **Enforce** — the doctrine (write-time) + `test_canonical_concerns.py` (suite) keep it in one home forever.

## Enforcement is TWO layers (why it reaches 100%)

Perfect *mechanical* dedup of arbitrary logic is undecidable — but *agentic* dedup is not (an LLM judges "these do the
same job" by meaning, exactly as the DECISIONS-ALWAYS-AGENTIC doctrine requires). So:

- **Registry layer (deterministic):** for a REGISTERED concern, a cheap home-check — its telltale operations must appear
  only in its home. `test_canonical_concerns.py` runs this in the suite; a new site fails it.
- **Agentic layer (complete):** the honestreview `canonical_concerns` doctrine judges EVERY diff for duplication of
  ANY existing capability, registered or not — the layer that catches the *novel* duplication a registry can't foresee.

The registry makes the known cases fast and deterministic; the agentic layer makes coverage complete.

## Where each piece lives (mirrors NAME_REGISTRY)

| Piece | Home | Role |
|---|---|---|
| The doctrine (LLM-judged, reusable) | **honestreview** (`canonical_concerns`) | repo-agnostic engine, write-time block |
| The registry `{concern → home}` | **this repo** `docs/CANONICAL_CONCERNS.json` | the per-repo data the doctrine reads |
| The suite gate | **this repo** `tests/test_canonical_concerns.py` | deterministic home-check for registered concerns |

## The doctrine prompt (honestreview `canonical_concerns`)

> You review a code diff for the single-source-of-truth invariant: every distinct CAPABILITY has exactly one home, and
> all callers use it. Inputs: the diff, `docs/CANONICAL_CONCERNS.json` (`{concern → home, description, telltale}`), and
> repo search.
> Decide, citing files/lines:
> 1. **Registered violation** — does the diff implement or inline a registered concern's logic in a file OTHER than its
>    home (e.g. it computes an output budget/ceiling itself instead of calling the canonical resolver)? → BLOCK: name the
>    concern, its home, the lines; instruct "call the home, do not re-implement."
> 2. **Novel duplication** — does it implement, BY MEANING (same job / decision / computation, not same text), a
>    capability that ALREADY exists elsewhere in the repo, even if unregistered? 2 places → FLAG (abstract to a home +
>    register); 3+ places → BLOCK (must abstract before landing).
> 3. **Not violations** — parsing/formatting trivia, genuinely-different logic that only looks similar, and PROTOCOLS (a
>    uniform contract each module implements by design). Do not flag these.
> Output one verdict per finding: the concern, its home (or a proposed home + module), the duplicate site(s), and the
> fix. Empty when the diff introduces no duplication. Prefer finding a real duplication over reassurance.

## Registered concerns

See `docs/CANONICAL_CONCERNS.json`. Seed set (each already has, or is being consolidated to, one home):
`output_ceiling`, `output_budget`, `input_fits`, `effort_resolution`, `pricing`, `deliberate_stop`, `admission_state`.

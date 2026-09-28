# INTENT — llm-spendguard (client)

**What this is.** llm-spendguard is *agentic forensic accounting for LLM and compute spend*. It runs where the
work happens — a CLI, an import-time gate, and a local ledger — and answers three questions with **evidence**,
not numbers you have to take on faith:

- **ATTRIBUTION** — where did every dollar go? (project · org · conversation · model · intent)
- **DISCOVERY** — what spend happened that you didn't know about? (ungated calls, remote/GPU compute, pre-ledger history)
- **CONTEXT** — what was it *for*, and was it worth it? (the job, the outcome, quality-at-cost)

**The mission is that those three are CORRECT.** If attribution/discovery/context are wrong, nothing downstream —
dashboards, org rollups, savings claims — has any value. Every number is cross-checked against **ground truth**
(the provider's actual bill + known repos), never against fixtures rigged to pass.

**What it does.**
1. **Gates every LLM/embeddings call.** `import spendguard; spendguard.require()` fails **closed** — ungoverned code
   can't run silently. Estimate-before-spend on every paid batch; never hardcode a price (only `pricing.py`).
2. **Routes batchable work across $0 subscription lanes** (claude-code · codex · gemini · zai) instead of paying the
   metered API — the biggest cost lever when a plan is capped. Each lane↔metered pair is atomic and reasoning-floor pinned.
3. **Reconciles the local ledger to the provider's real bill** across **batch · realtime · GPU**, and **reconstructs
   realtime spend WITHOUT a provider admin key** — agentically, from conversation token records — so you know your
   realtime spend even when the provider offers no cooperation.
4. **Attributes every charge agentically** to project/org/conversation. Decisions about *meaning* are always an LLM's,
   never regex.
5. **Records EVERY call and guarantees delivery.** Every call — a metered success, a $0-lane hit, or a failure — lands
   on the queryable ledger with its full forensic outcome (kind · http_status · provider_error · retry_of · attempts ·
   disposition); a failure is never dropped (that was the foundational gap this program closed). A durable work queue
   retries a **TRANSIENT** fault (transport / overloaded) up to 10× while a **DETERMINISTIC** one (bad payload, refusal,
   unfunded, truncation, empty, schema) fails **fast** — no wasted retries. A deliberate spend refusal is a first-class
   outcome (**GATE_REFUSED**), NEVER retried against the cap that refused it; an unknown provider is PREFLIGHT_UNMET,
   never a KeyError re-run. Exhaustion after the retries is **LOUD**. A periodic planner predicts a 429 from the backlog
   vs the vendor's TPM ceiling and **paces or batches BEFORE it happens**.
6. **Keeps model facts CURRENT and SOURCED, never guessed.** Price · token upper-bounds · capabilities (vision / response
   schema / function-calling / …) are one authored record per model (`model_catalog`, the SSOT; `prices.json` is
   generated, never hand-edited), sourced from LiteLLM's community dataset — a verifiable **FACT**, because an LLM judge
   cannot know models past its cutoff. The org server's daily-refreshed `/v1/models` is the authoritative "first place to
   check"; the client checks it **first** and **FAILS OPEN** to its own local LiteLLM sync when it is unreachable.

**What it is NOT.** Not a proxy or gateway (it gates in-process). Not the dashboard — that's the **server**
(`llm-spendguard-server`), which aggregates what this client pushes and **never recomputes a cost**. Not a heuristic:
cost is controlled by the *rails* (gate · estimate-first · batch · cache · cheap-lane), never by swapping the LLM for a
keyword hack. A $0 attribution that is wrong is worth less than nothing.

**Load-bearing guarantees** (enforced by tests/lints/hooks, not memory — the invariants a well-meaning author could
silently betray while the code still "works"; the honestreview intent-alignment axis judges the repo against THESE):

- *Meaning is agentic.* Every judgement about CONTENT — attribution ("what work/project is this?"), quality ("is this
  output good?"), entity resolution, "did the model actually answer?" — is an LLM's; cost is NEVER lowered by
  de-agentic-ifying one. Regex/substring is only for PARSING a fixed CONTRACT: a model-id token, a batch id, a timestamp,
  a Retry-After header, OR **a provider's own structured failure signal** — the HTTP status code plus its documented
  error identifier (`insufficient_quota`, `content_policy_violation`, …) mapped to a delivery class (unfunded / policy /
  overloaded / transport / truncated). That mapping is CONSERVATIVE by construction: an unrecognized error stays a
  RETRYABLE transport fault, so a miss fails SAFE (a wasted retry, never a suppressed one or a masked charge) — it is
  reading the vendor's declared failure, not interpreting free prose or judging whether a 200-OK answer is *good*. Where
  any such parse could be AMBIGUOUS (two models named near one usage block) it is REFUSED and surfaced, never guessed
  first-hit-wins.
- *Fail closed.* `spendguard.require()` raises unless the gate is enforcing; ungoverned LLM code can't run silently.
- *Estimate before spend* (a separate $0 estimate on every paid batch; a projection over the cap REFUSES) · *never
  hardcode a price* (only `pricing.py` / the generated `prices.json`).
- *Every call is on the queryable ledger whatever its fate* — metered success, $0 lane, or failure — with its full
  forensic outcome (kind · http_status · provider_error · attempts · disposition); a retry is its OWN row linked to the
  root (`retry_of`). A failure is never dropped.
- *Class-aware delivery.* Only a TRANSIENT fault (transport / overloaded) retries — up to 10× on the durable queue; a
  DETERMINISTIC one (payload / refusal / unfunded / truncation / empty / schema) fails FAST; a deliberate spend refusal
  (**GATE_REFUSED**) and an unknown provider (**PREFLIGHT_UNMET**) are NEVER retried. Exhaustion is LOUD.
- *No user sees a 429.* A universal admission layer paces per-vendor TPM, self-calibrates from 429 **and** success
  headers, and PARKS a rate-blocked task; a batch fan can't starve realtime of a governor slot; the planner predicts the
  ceiling breach and paces/batches BEFORE it happens.
- *A $0-only call never pays to RETRY an unsuitable task.* With `no_metered_fallback` (the `--refuse-billed` caller), a
  lane miss on an unsuitable task — empty / off-shape / oversized / a transient quota it resets from — stays a $0 miss
  row, never a surprise metered charge. The ONE deliberate exception: a lane that is infrastructure-DOWN (auth expired /
  CLI crash / rejected model) still fails over (another $0 lane first, then metered) — losing work to a dead lane is the
  silent-empty this program exists to kill. Which case it is a STRUCTURAL split on the miss reason (`lane_error`/`auth`
  vs an unsuitable-task reason), never a parse of the error prose (that is the agentic remediation's job).
- *spendguard OWNS the output budget.* The caller's `max_tokens` is IGNORED; the budget is the model's published ceiling,
  never below the 32K floor unless a lower one is authoritatively published — ONE home across every send path. The
  silent-truncation class is killed at the root.
- *Never truncate the evidence a judgement reads.* A reviewer / judge / classifier — and an embedding's input — is sent
  WHOLE (stream or chunk if large), never a silent head-slice.
- *Every spend control is REAL at the moment it matters, or fails LOUD.* A pinned reasoning effort is honored or refused
  (never silently dropped); `budget_usd` is a running cap enforced at the CALL DOOR (not just the upfront estimate); a
  reasoning call is never cut mid-thought before its cost is measured; reasoning tokens are priced as output.
- *Model facts are one SOURCED record* (`model_catalog` SSOT — price · limits · capabilities from LiteLLM, a verifiable
  FACT never a guess); every derived copy (`prices.json`) is GENERATED, never hand-edited.
- *One concept, one home* (the canonical-concern registry): a capability is implemented once, not re-derived per caller.
- *Names are unique and semantically true* across the repo.
- *A deliberate stop is never downgraded to a fail-open "keep going" or a fake success.* A spend refusal / deadline
  becomes a NON-RETRYABLE, non-ok outcome whose `.text` RAISES (GATE_REFUSED / DEADLINE_EXCEEDED) — recorded, never a
  silent retry or empty-read-as-success — and a fan/queue-level stop HALTS the batch.

**Cost is controlled by the RAILS** — the gate · estimate-first · Batch-API packing · caching · the cheapest
(model, effort) whose MEASURED quality holds (best-value, floor-preserving) · TRUE-marginal-cost lane-vs-batch-vs-combo
routing · capability-aware routing (a strict schema a $0 lane can't guarantee goes to the vendor's enforcing meter) ·
the whole-job contract (`run_jobs(jobs, goal)` — hand spendguard the job set + goal; it plans, budget-gates, executes,
returns; the caller never hand-tunes metered_only/batch/lanes/model) — NEVER by swapping the LLM for a keyword hack. A
$0 attribution that is wrong is worth less than nothing.

Full operating doctrine: `CLAUDE.md`. Architecture: `docs/AGENTIC.md` (agentic attribution/reconcile) +
`docs/ARCHITECTURE.md` (gate/rails) + `docs/CANONICAL_CONCERNS.json` (one-home registry).

**Boundary with the server.** This client is **measurement + source of truth** (the gate, the reconcile, the forensic
attribution). The server is **aggregation + presentation + billing**. A cost is computed here, once, and pushed up as a
roll-up; the server stores `spend_micros` exactly as attested and never re-derives it. The one thing the server IS
authoritative for is **org POLICY**: spending caps it sets flow DOWN via `pull_policy` — *advisory* (the org's suggestion;
the dev's own local cap still wins — partner, not supervisor) or *enforced* (a hard ceiling the local config can only
TIGHTEN, never loosen). That is governance, not cost re-derivation: the org sets limits; the client still measures and
attributes every dollar itself. Guarded roll-ups pushed up are SCOPED to this connection's own project(s)/org — an org
that resolves to no projects pushes NOTHING rather than over-sharing (fail-closed, never fail-open).

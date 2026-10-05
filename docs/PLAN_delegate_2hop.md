# PLAN — `spendguard delegate` : the 2-hop (classify → route to a cheaper provider → receipt)

## Intent (why this exists)

While the Anthropic weekly plan is CAPPED and the account is on **paid per-token overage**, the single biggest cost
lever is to stop spending Claude tokens on work another subscription can do for $0. The user asked: *"is this request
or next step able to be done well by codex/sol/luna etc — then push to that and use that instead of expensive Claude
calls."* That is a **2-hop**: (1) **classify** whether a task can be done well off-Claude and how, then (2) **route** it
to that provider and return the result + a receipt.

**Honest architecture boundary (state it, don't paper over it):** spendguard CANNOT reroute the Claude Code agent's
OWN conversational turns — those bill Anthropic directly and no flag redirects them. What `delegate` DOES is give the
agent (or user) an explicit command to hand a **self-contained sub-task** to another provider's plan, so the heavy
tokens bill THAT subscription. This command is the sanctioned offload path from CLAUDE.md ("delegate to that provider's
agent CLI — `codex exec`, `agy`, `kimi` — and collect its result").

## What already exists — REUSE, do not rebuild (CLAUDE.md: extend-don't-rebuild, check-catalog-first)

- `lane_balance.delegate(task, system=, lanes=, reasoning=, intent=, …)` (`lane_balance.py:205`) — one-shot a single
  prompt onto the cheapest viable **idle $0 lane**. This IS hop-2 for one-shot comprehension. **Keep its name**; the
  new code CALLS it. Do NOT add a second `def delegate` (naming-uniqueness rule — `grep -c "def delegate"` must stay 1).
- `codex_exec.run_prompt(prompt, system=, model=, timeout=, …)` (`codex_exec.py:273`) — headless `codex exec … --json`
  on the ChatGPT plan, with real usage capture + warm-daemon + plugin-disable. This is hop-2 for an **agentic** OpenAI
  sub-task. Reuse it (add a sandbox/workspace knob if it does not already expose one; see below).
- `lane_balance.bulk_delegate(...)` (`lane_balance.py:386`) + `estimate_fan(...)` (`lane_balance.py:1203`) — the fan
  path and its ZERO-SPEND estimator. `delegate` is the SINGLE-task sibling; reuse `estimate_fan`'s costing style for
  the dry-run estimate.
- `guard.record_decision(why=…)` — record the routing decision so it is auditable (`decisions.why`).
- `config.advisor.lane_models` — the representative model per plan (codex→{cheap:luna,strong:sol}, gemini, zai-coding).
  The provider/model menu comes FROM HERE, never hardcoded.
- The savings/receipt machinery behind `spendguard savings` / `spendguard_savings` and `spend_overview` — the receipt
  reuses it; do not invent a new savings store.

## The build

### CLI surface (new top-level command)
```
spendguard delegate "<task>" [--files a.py,b.md] [--intent <tag>] [--provider codex|gemini|kimi|auto]
                             [--estimate] [--yes] [--timeout S]
```
- Default (no `--yes`) = **dry-run / estimate-first** (CLAUDE.md API-spend protocol): print the classification, the
  chosen route, the est cost on the sub-plan, and the est $ it SAVES vs running the same work on Claude overage —
  then stop. `--yes` executes. `--estimate` forces dry-run even with `--yes`.
- `--provider auto` (default) lets hop-1 choose; an explicit provider skips the provider half of classification but
  still classifies one-shot-vs-agentic-vs-needs-claude.

### Hop 1 — classify (AGENTIC, never regex — CLAUDE.md decisions-are-agentic)
A small structured LLM call, itself run on a **$0 lane** (via `lane_balance.delegate`, intent `spendguard:classify`)
so the classifier costs nothing on Anthropic. It returns a validated object:
```
{ "kind": "oneshot" | "agentic" | "needs_claude",
  "provider": "<a plan name from advisor.lane_models, or null>",
  "self_contained": true|false,
  "why": "<one sentence>" }
```
- `oneshot` = an independent one-shot prompt (classify / extract / summarize / answer) — lane-routable.
- `agentic` = a self-contained multi-step sub-task (edit these files + run tests; implement X) — needs a provider
  **agent CLI**, not a chat completion.
- `needs_claude` = NOT self-contained / needs the live interactive session, shared context, or this repo's running
  state — cannot be offloaded; say so.
The decision is the model's. Do NOT keyword-match the task string to decide kind/provider. The classifier reads the
WHOLE task + any `--files` contents (read files whole — no truncation of the evidence the decision consumes).

### Hop 2 — route
- `oneshot`  → `lane_balance.delegate(task, intent=…, …)` → text result.
- `agentic`  → the chosen provider's agent CLI as a self-contained run:
  - `codex`  → `codex_exec.run_prompt(...)` with a **workspace-write** sandbox so it can actually do the task
    (`codex exec --sandbox workspace-write --approve-for-me`). If `run_prompt` today only supports a read-only
    meta-reasoning run, add an explicit `sandbox="workspace-write"` (or `writable=True`) parameter — default stays
    the current read-only behaviour so existing advisor.executor=codex callers are unchanged.
  - `gemini` → `agy`, `kimi` → `kimi` (same self-contained-run shape; a thin per-provider runner, names unique).
- `needs_claude` → do not execute; return a typed refusal with `why` (this is the deliberate-refusal pattern, never
  fail-open into an expensive Claude call the user was trying to avoid).

### Receipt (CLAUDE.md rule 7 — SPLIT axes, NEVER sum)
On execute, print the result then a receipt line of the form:
`delegated to <plan> (<model>)  ::  est-value $V on <plan>  ::  saved ~$S vs Claude overage`
where **$V** = est-value delivered on the sub-plan (NOT billed), **$S** = the counterfactual Claude-overage cost of the
same tokens (from the ledger's measured overage rate — look it up, never hardcode a $/tok). Real billed $ for the sub
call is $0 (plan-covered); if a provider leg ever falls to metered, show that as a separate API-$ component. Record the
saving through the existing savings store (source `delegate`) so `spendguard savings` picks it up.

## Naming (CLAUDE.md naming-uniqueness — adjudicate before creating)
- New module `delegate_router.py` (sibling of `lane_balance.py`). Proposed unique names (verify each
  `grep -c "def <name>"` == 0 before adding): `classify_task()` (hop 1), `delegate_task()` (orchestrator returning a
  result dict), `_route_agentic()` (provider-CLI dispatch). The CLI handler wires `spendguard delegate` → `delegate_task`.
- Do NOT reuse `delegate` / `bulk_delegate` / `run_prompt` as new definitions — call them.

## Constraints (hard rules — all apply)
- **No hardcoding**: providers, models, lane names, $/tok, overage rate, caps — all from config / the ledger / pricing,
  never literals in source. The provider menu is `advisor.lane_models`.
- **No shortcuts / no truncation**: the classifier sees the whole task + whole `--files`; size any structured reply's
  token budget for the whole object.
- **Estimate before spend**: dry-run is the default; `--yes` to execute; refuse over any intent cap.
- **Decisions agentic**: kind/provider are model judgements, not regex.
- **Write source via the editor, unique semantic names, offline tests set their own fake key.**

## Verification (must be green before this is done)
- New unit tests (offline, stub the lane + `codex_exec.run_prompt`, no live spend — pattern of
  `tests/test_lane_fallback_is_error_aware.py`): classify returns a validated object for each kind; route dispatches
  oneshot→lane, agentic→codex runner, needs_claude→refuse (no execution); dry-run spends nothing; receipt splits axes
  and never sums; names are unique.
- `spendguard deploy` gate green: ruff + chunked pytest + name-uniqueness + `spendguard audit --ci`.
- **Do NOT push or open a PR.** Implement on a branch `delegate-2hop`, run the gate, and leave the branch + a short
  summary of what changed for human review.

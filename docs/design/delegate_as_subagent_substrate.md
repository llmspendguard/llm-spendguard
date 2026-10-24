# Make `spendguard delegate` the default "subagent" path — brief for a codex delegate run

**Filed:** 2026-10-24 · **Repo:** llm-spendguard · **Executor:** codex (self-contained; this is code work)
**Companion fact:** on this machine codex already registers the shared loopback MCP servers
(`~/.codex/config.toml`: `symgrep` :4911, `7thsense` :4910, `honestreview` :4912, `spendguard` :4913,
`ccwatch` :4900, `ccoffload` stdio). This brief does NOT ask you to wire them — it asks you to PROVE a
delegated run actually reaches them and to stop the ones gated by `approval_mode` from silently degrading.

Repo doctrine applies verbatim (`CLAUDE.md`): **#0 GROUND before you act**, **#1 decisions about meaning are
agentic, never regex**, **anti-amnesia — every lesson ends as a test, not a comment**, **estimate-first / under
the gate**, **never hardcode** (ids, ports, paths, model names, thresholds → config/env/constants), and the
**split receipt** (real-$ vs est-value vs saved, never summed). A claim of "works" / "cheaper" without a
before/after measurement is not accepted.

---

## Why this exists (the goal, stated once)

The orchestrating agent (Claude Code) wants to hand off a *self-contained* unit of work the way it would spawn a
subagent — heavy work happens elsewhere, only a small conclusion returns — **but billed to codex's plan, not the
Anthropic plan.** A Claude Task/Agent subagent gives the residency win (transcript stays out of the main
context) yet still bills Anthropic and cannot be rerouted by any flag. `spendguard delegate` already IS "a
subagent that runs on codex": it classifies the task, hands a self-contained one to `codex exec`, collects the
result, returns a conclusion. The substrate exists (see `project_spendguard_agentic_delegate_substrate`); what
is missing is that it is not yet the *trustworthy default*. This dispatch closes that gap.

Scope is **self-contained** subwork only. `spendguard_delegate` already documents "it does NOT reroute this
calling agent's own turns" — keep that invariant. Interactive, decision-coupled, turn-by-turn work stays on the
main thread; this brief does not touch it.

---

## 0. GROUND FIRST (do this and state the answers before writing code)

1. **What already implements delegation?** Read the `delegate` verb end to end — CLI (`spendguard delegate`),
   the MCP tool `spendguard_delegate(task, files?, intent?, provider?, execute?)`, the classifier it calls, and
   `discover_agentic` / whatever picks the provider lane. Name the files and functions. Extend them; do NOT
   build a parallel path. If you claim a capability is missing, prove it's missing first (grep + read).
2. **Is the classify step agentic and across all providers?** The codex-able vs must-stay decision is a
   judgement about meaning → it MUST be an LLM on a $0 lane, never a regex/keyword list. Confirm the current
   state and record which it is.
3. **Cross-check against ground truth**, not fixtures: a delegated run's billing must land on codex's plan in
   the ledger (Σ attributed to codex), and $0 on the Anthropic lane. 
4. **What guard makes each fix un-regressable?** Each item below names its test. No item is "done" as prose.

---

## 1. Prove the tool envelope — and stop `approval_mode` tools from silently degrading

**Measured setup facts (verify, do not trust this doc):** codex runs `sandbox_mode = "workspace-write"`. The
shared MCP servers are loopback HTTP. Two of them gate tools behind approval:
`[mcp_servers.symgrep.tools.search]` and `[...indexed_roots]` and `[mcp_servers.honestreview.tools.warm_daemon_metadata]`
are `approval_mode = "approve"`. In a **headless** `codex exec` delegate run there is no human to approve, so
those calls are DENIED and the agent silently falls back (for symgrep, to plain `rg` with no expansion safety
net). This is the single thing that makes the delegation envelope look narrower than it is.

Do, in order, and paste evidence for each:

- **Prove reachability.** From inside a real delegated `codex exec` run (sandbox on), call one tool on each of
  `symgrep`, `7thsense`, `honestreview`, `spendguard` and show it returned real data, not a connection error.
  If the sandbox blocks loopback network, **say so verbatim** with the exact error — that is then the finding,
  and the fix (enabling loopback in `[sandbox_workspace_write]`) is a separate, named change, not a silent one.
- **Fix the silent-degrade.** Decide the canonical handling for approval-gated read-only tools in a *delegated*
  context and implement ONE:
  (a) the delegate invocation passes codex's non-interactive approval path (e.g. `--approve-for-me` / the
  documented bypass) **scoped to the read-only code-intel tools only**; or
  (b) a delegated-context codex profile that sets `approval_mode` to auto for `symgrep.search` / `indexed_roots`
  / `7thsense` reads while leaving anything with side effects gated.
  Whichever you pick: a delegated run that *cannot* reach symgrep must **fail loud or record a NAMED degraded
  result** — never quietly answer from `rg` while reporting success. (This is the `symgrep` degraded-search
  doctrine: degraded search must be visible, never silent.)
- **Guard:** a test that runs a delegated task whose correct answer *requires* a gated tool (e.g. a symgrep
  zero-hit expansion) and FAILS if the result came from the fallback. Without it this is prose.

The must-stay-Claude set after this item is only: (i) work coupled to the live main conversation, and (ii)
driving the Claude desktop app (`ccd_*`, the in-app browser pane). Everything else is codex-able. Encode that
boundary where the classifier can read it — as data/config, not as a hardcoded list in a function body.

## 2. The classifier — agentic codex-able vs must-stay-Claude (never regex)

The decision "can this subtask run entirely on codex, or must it stay a Claude subagent?" is a judgement about
meaning. It goes to an LLM on a $0 lane (verdict cached per task fingerprint — see the resume/fingerprint
doctrine `feedback_content_addressed_resume_needs_a_plan_fingerprint`). It must answer one of:
`codex` (self-contained) · `claude-subagent` (needs this host's live app/conversation context) ·
`lane-route` (batchable one-shot comprehension — hand to `spendguard comprehend`, not an agent at all) ·
`main-thread` (interactive). Return the verdict + a one-line reason + the fingerprint.

**Guard:** a labelled fixture set of tasks with known correct routes; the test fails if the classifier is
swapped for, or regresses to, a keyword/regex rule (assert the decision path calls the model), and reports
accuracy against the labels. Do not tune a threshold by hand — measure.

## 3. Reliability — so "default" is safe

Each is a known edge; fix and prove:

- **Fail CLOSED, never open.** If the codex lane is unavailable/capped, the delegate must fall back to a Claude
  subagent (or return "cannot delegate, run here") — it must NEVER fail-open into a metered Anthropic call the
  caller thought was offloaded. Precheck lane health first (`spendguard_health` / `spendguard lanes --usage`);
  route only to a READY lane; name the ready alternatives when the preferred one is down.
  (`feedback_deliberate_refusal_never_failopen`.)
- **`--cwd` and `--provider` pinning** (`reference_spendguard_delegate_cwd_and_provider_pin`): a delegated run
  must execute in the intended repo and on the intended provider, not inherit an ambient default. Verify both
  are threaded through CLI → MCP → `codex exec`.
- **Warm-daemon wedge** (`reference_spendguard_codex_warm_daemon_wedge_bounce`): confirm a wedged codex
  app-server / stale socket does not hang a delegate call uncancellably. A hung endpoint must fail fast on a
  bounded timeout (as `spendguard_health` already does), not wedge the caller.
- **`needs_claude` is a BRIEF defect, not a routing outcome** (`feedback...codex operating mode`): if codex
  bounces work back as needing Claude, that means the task wasn't self-contained — surface it as a defect to
  fix the brief, don't silently re-run on Anthropic.

**Guard:** tests for lane-down → fail-closed, timeout → fast-fail, and cwd/provider actually applied.

## 4. The receipt — split, never summed

Every delegate result prints a receipt in the mandated form, three axes kept separate:

```
Real $ = $A codex API/overage + $S Subscription (plan)   ::   Est sub value $V   ::   Saved vs Anthropic-overage $R
```

`Saved` = the counterfactual cost had this run executed on the capped/overage Anthropic plan instead of codex's
subscription. This is the number that justifies making delegate the default. Keep `Saved` on its own axis —
never fold it into real-$ or est-value. Source the saving from the ledger counterfactual, not from logs
(`feedback_verify_billed_cost_via_ledger_not_logs`). Prices only from `spendguard.pricing` — no hardcoded rates.

## 5. Make it the default (ergonomics)

The front door stays `spendguard delegate "<task>"` (CLI) and `spendguard_delegate(...)` (MCP). Deliverable:
a one-call path where the orchestrator hands a self-contained task and gets back ≤10 lines (verdict/result +
evidence as `file:line` + what it could not determine + the split receipt), with routing (item 2) and
fail-closed (item 3) automatic. Do not intercept or reroute the Claude Task tool — that fights the
architecture; the win is that `delegate` is trustworthy enough to reach for by default.

---

## Division of responsibility

- **This repo (spendguard)** owns: the classifier (item 2), fail-closed routing + health precheck + pinning
  (item 3), the receipt (item 4), the front door (item 5), and the delegate-context approval handling (item 1b).
- **The shared MCP servers** (symgrep/7thsense/honestreview) are already running and shared; this brief does
  not change them. If item 1 finds the sandbox blocks loopback, that config change is named and handed back,
  not made silently.

## Verify green

Run every command and paste **full** output — no piping, no truncation (`feedback_no_shortcuts`,
never-truncate-evidence):

- `spendguard doctor` → must print `ENFORCING HERE: YES`.
- the repo's offline suite, before and after.
- **item 1:** a delegated `codex exec` run showing a live call to each of symgrep/7thsense/honestreview/
  spendguard returning real data; and the degraded-path test (requires a gated tool, fails if it used fallback).
- **item 2:** classifier accuracy against the labelled route fixtures.
- **item 3:** lane-down → fail-closed (NOT an Anthropic call); timeout → fast-fail; cwd/provider applied.
- **item 4:** a real delegate run's receipt in the split form, with `Saved` sourced from the ledger.
- a ground-truth check: the ledger shows the delegated run billed to codex and **$0 on the Anthropic lane.**

If the sandbox denies loopback network, `~/.spendguard` writes, or a port bind, **say so verbatim** — the host
re-runs it. Do not fabricate a pass.

🤖 Generated with [Claude Code](https://claude.com/claude-code)

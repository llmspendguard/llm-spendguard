<!-- spendguard:rule:begin -->
## LLM Cost Governance — MANDATORY (llm-spendguard)

Any code in this project that calls an LLM or embeddings API (OpenAI **or** Anthropic) MUST go through
**llm-spendguard**. This is a hard rule that OVERRIDES default behavior — never write or run ungated LLM code.

When you WRITE or RUN code that calls an LLM:
1. **Run it under a gated interpreter.** Use a venv whose `sitecustomize.py` loads the gate (or a python whose
   user-site has the spendguard `usercustomize`). Never bare/system `python3` that isn't gated. Verify with
   **`spendguard doctor`** — it must print `ENFORCING HERE: YES`.
2. **Fail closed.** Put `import spendguard; spendguard.require()` at the top of the script. It raises if the
   gate isn't actually enforcing in that interpreter, so a bypass can't run silently.
3. **Never hardcode prices.** Get $/token only from `spendguard.pricing` (or the repo's `pricing.py`).
4. **Estimate before you spend.** For any paid batch, do a SEPARATE zero-spend estimate run (count + $ est),
   confirm, then submit. Never cancel/kill a running job as cost control — completed requests still bill.
5. Prefer the **Batch API** for non-interactive work; keep a per-job cost estimate + approval for large batches.
6. **Surface the receipt.** After substantive LLM/spend work in a turn, show the contextual spend receipt —
   `spendguard receipt` (scoped to THIS repo + its proportional plan share; `--all` expands to every repo). In the
   desktop/web app there is no auto status line, so this is how the running tally stays visible each turn; in a
   terminal the status line does it automatically.
7. **ALWAYS split billed $ from est-value — NEVER sum them (hard rule, for spendguard AND for me in chat).** A single
   summed cost number causes huge confusion. Whenever cost is shown, display the REAL dollars broken into named
   components, then the est-value as a SEPARATE axis after a `::`:
   `Total $X = $A API + $S Subscription (plan name) + $R Remote (provider)  ::  Est sub value $V`
   where **REAL $ = API (per-token billing) + Subscription (flat plan fee: Anthropic Max, OpenAI Pro, …) + Remote
   compute (Vast.ai, …)** — the actual money out the door — and **Est sub value** = the value of subscription-covered
   usage (Claude Code / claude.ai / Codex plan usage), which is NOT billed and is never added into the real total.
   The subscription is BOTH a real cost (its flat fee) AND the source of the est-value (what it delivered) — show both.
   Example: `$3,376 = $2,189 API + $200 Subscription (Anthropic Max) + $987 Remote (Vast.ai)  ::  Est sub value $21K`.
   Per org, the Subscription line is that org's proportional share (by plan-usage). Never collapse to one mixed total.
8. **Prefer lane-routed comprehension over Claude sub-agents (biggest cost lever when the plan is capped).** For
   batchable comprehension / doc-mining / gap-analysis, run a spendguard-gated script that fans the work across your
   $0 subscription lanes — `spendguard comprehend <globs> --intent <job>` (or `spendguard ask`, or a script calling
   `adapters.call(..., reasoning="best-value", intent=...)`) — NOT the coding agent's own sub-agents. A sub-agent runs
   ONLY on the Claude/Anthropic plan: it bills the Max plan (hitting weekly overage) and cannot use the codex / gemini
   / zai plans, whereas a lane-routed script does the same work at $0 on another subscription. Matters most when the
   Claude plan is near its weekly cap (check `spendguard lanes --usage`): spendguard's router already sheds eligible
   tagged intents off an exhausted lane onto the others, but sub-agents never reach that routing.
   **The boundary:** BATCHABLE COMPREHENSION — classify / extract / summarize / gap-find across many files, each an
   INDEPENDENT one-shot prompt — lane-routes; INTERACTIVE code-editing, multi-step tool use, or worktrees do NOT
   (they are not one-shot and can't ride a lane), so a Claude subagent is the right tool THERE. Don't force lane
   routing onto work that needs a real agent. **Auto-route the batchable kind:** set `advisor.default_reasoning`
   to `best-value` (or `SPENDGUARD_DEFAULT_REASONING=best-value`) so a LABELLED `adapters.call(intent=…)` that
   didn't pin a model picks the cheapest (model, effort) whose MEASURED quality held — no per-call opt-in.

Setup (one-time): `spendguard install-hook --venv <venv>` (or `--user --python <interp>` for system python),
then `spendguard doctor`. Surface the tally: `spendguard install-receipts` (terminal status line) and this rule
(desktop/web). Kill switch: `GATE_DISABLE=1` or `spendguard off`.
<!-- spendguard:rule:end -->

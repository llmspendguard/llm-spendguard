# Make every AI-assistant conversation use spendguard

The gate only enforces in the interpreter it loaded in, so the real risk is that **code an assistant
writes (or runs) doesn't route through spendguard at all** — it spins up a script under some ungated python
and spends silently. Close that at the source: give the assistant a standing *rule* that any LLM code it
builds must go through spendguard, in every conversation, automatically.

In Claude Code (and Cursor, etc.) that standing rule is **`CLAUDE.md`** — it's loaded into the context of
every session for a project (and `~/.claude/CLAUDE.md` for *all* projects). `install-rule` writes the rule
there for you.

## One command

```bash
spendguard install-rule --global          # ~/.claude/CLAUDE.md — applies to every project on this machine
spendguard install-rule --project .       # ./CLAUDE.md — just this repo (default if no flag)
```

It writes a marked block (`<!-- spendguard:rule:begin … end -->`) so it's **idempotent**: re-running
*updates* the block in place instead of duplicating it, and it appends below any content you already have.
Re-run it after upgrading spendguard to pick up wording changes.

## What the rule tells the assistant

> Any code that calls an LLM/embeddings API (OpenAI or Anthropic) MUST go through llm-spendguard:
> 1. Run it under a **gated interpreter** (venv with the `sitecustomize` hook, or a python with the
>    `usercustomize` hook). Verify with `spendguard doctor` → `ENFORCING HERE: YES`.
> 2. Put `import spendguard; spendguard.require()` at the top — fail-closed if the gate isn't enforcing.
> 3. Get prices only from `spendguard.pricing` — never hardcode a $/token number.
> 4. Estimate (separate zero-spend run) before any paid batch; never cancel a running job as cost control.
> 5. Prefer the Batch API for non-interactive work.

## The two layers, together

`install-rule` is the **generation-time** guard (the assistant writes gated code from the start).
It pairs with the **run-time** guards so a slip is still caught:

| Layer | Command | Guarantees |
|---|---|---|
| Assistant writes gated code | `spendguard install-rule --global` | every conversation is told to wire in spendguard |
| Venv auto-loads the gate | `spendguard install-hook --venv <v>` | every process in that venv is gated |
| System python auto-loads it | `spendguard install-hook --user --python <interp>` | bare `python3 …` is gated (PEP 668-safe, no pip) |
| Script refuses if ungated | `spendguard.require()` at top of script | fail-closed, no silent bypass |
| See it now | `spendguard doctor` | prints `ENFORCING HERE: YES/NO` |
| Catch what slipped | `spendguard reconcile-ledger` | provider billing vs local ledger → leaks within a day |

See [ARCHITECTURE.md §6](ARCHITECTURE.md) for the full enforcement-levels discussion (the only *guarantee*
across any language/machine is the key-holding proxy on the roadmap).

## Slash commands

`spendguard install-skills` deploys the spendguard slash-commands into `~/.claude/skills/`:

| skill | what it runs |
|---|---|
| `/spend` | quick status: today/7d/month, leak check, top learnings, SaaS connection |
| `/spendguard-reconcile` | make the ledger TRUE vs provider billing; surface leaks; push the dashboard |
| `/spendguard-learn` | backfill the cost+quality corpus; run the advisor + backtests |
| `/spendguard-prompts` | the prompt lab: lint → batch-1 → graded A/B → promote (docs/PROMPT-EFFICIENCY.md) |
| `/spendguard-close` | the monthly close: truth push (owner) → close view → the org statement + residual |

All are zero-model-spend by default; experiments are caged + estimate-first.

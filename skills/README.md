# `skills/` — Claude skills

Self-contained skills a Claude agent can invoke to drive spendguard.

| skill | invoke | what it does |
|---|---|---|
| [spend](spend/SKILL.md) | `/spend` | Quick status — spend totals (today/7d/month), the ledger-leak check, and the top cost learnings. Read-only. |
| [spendguard-learn](spendguard-learn/SKILL.md) | `/spendguard-learn` | Drive the learning advisor end-to-end: backfill from real history, mine intents + the conversation playbook, surface confidence-scored insights / a per-intent recommendation. |

**Install as slash-commands:** `spendguard install-skills` copies these into `~/.claude/skills/`, so they
work as `/spend` / `/spendguard-learn` in Claude Code (CLI **and** the VS Code extension). Everything they do
is also a plain `spendguard <command>`; the skills just make it one keystroke for an agent.

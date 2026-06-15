# `skills/` — Claude skills

Self-contained skills a Claude agent can invoke to drive spendguard.

| skill | what it does |
|---|---|
| [spendguard-learn](spendguard-learn/SKILL.md) | Drive the learning advisor end-to-end: backfill the corpus from real history, mine intents + the conversation playbook, and surface confidence-scored insights / a per-intent recommendation — so the advisor's history-aware guidance is one invocation away. |

These are optional. Everything they do is also available as `spendguard <command>` (see the root
[README](../README.md)); the skill just packages the workflow for an agent.

# Roadmap — individuals → teams → orgs (partner mindset)

The guiding principle: **partner, not supervisor.** Every user sets their own limits and keeps their own
ledger. Teams and orgs get **visibility and pooled learnings by roll-up**, not control. Nobody's caps are
locked from above; the value is shared sight and shared knowledge, opt-in.

## Tiers

| Tier | Who | What they get | How |
|---|---|---|---|
| **I** (developer) | one person | local gate + caps **they** set, local ledger, local learnings | `pip install llm-spendguard` (this repo) — already shipped |
| **We** (team) | a team | see each other's usage, pooled + aggregated learnings, team rollup report | set a **team id** in config + opt-in sync to the SaaS |
| **Us** (org) | an org | org-wide visibility, aggregate advice across teams, org rollup | set an **org id** + members opt-in sync |

Roll-up is **additive and opt-in**: a user chooses how much to share (full usage / costs-only /
scrubbed-learnings-only) in their own config. The default is private; sharing is a choice, and it's *their*
choice. Greater visibility is something a user turns **on** — the partner mindset.

## Two repos

- **`llm-spendguard` (this repo, open):** the client — gate, pricing, ledger, advisor, learnings. Each user
  has their **own** ledger here. It gains a small, optional **sync** capability: with a `team_id`/`org_id` +
  token in config, it pushes its ledger summary + scrubbed learnings to the SaaS for aggregation. No server
  code lives here.
- **`llm-spendguard-server` (separate repo, the SaaS — e.g. llmseg.ai):** login / create account / credit
  card / define team & org → get **team/org ids** to drop in your client config. It ingests members' pushed
  data and provides: cross-member **visibility** dashboards, **aggregate learnings**, **aggregate advice**,
  and rollup reporting. This is the open-core → hosted-collaboration layer; **build it as its own repo.**

The client never depends on the server (works fully standalone, fail-open). The server is purely additive
visibility/aggregation over what users opt to send.

### Client seam (already mostly present)
- Config knobs to add: `team_id`, `org_id`, `sync.endpoint`, `sync.token`, `sync.visibility` (full | costs | learnings).
- Transport reuses the existing `emit` webhook + `insights export` (scrubbed) — the SaaS ingest endpoint is
  just another sink. Each user's own SQLite ledger stays the source of truth; the SaaS aggregates copies.
- Scrub rules per visibility tier (never send prompts/keys upward; team can keep intent names, org gets
  scrubbed, community fully scrubbed) — the `scope` field + scrubber already exist.

## Access — slash commands (shipped)
`spendguard install-skills` deploys `/spend` (quick status: totals + leak + top learnings) and
`/spendguard-learn` (the advisor) into `~/.claude/skills/`, so they work as **slash-commands in Claude Code
(CLI + the VS Code extension)**. A native VS Code panel is possible later but optional — the skills cover
both surfaces today.

## Build order
1. **(done)** Client: gate / pricing / ledger / advisor / learnings / reconcile-ledger / report / slash-commands.
2. Client **sync** seam — config knobs + push ledger-summary & scrubbed learnings to a configurable endpoint
   (works against a flat file / bucket first, no server needed).
3. **`llm-spendguard-server`** (separate repo): account/team/org, ingest, aggregate visibility + learnings +
   advice, billing. llmseg.ai.
4. Publish client to PyPI; orgs use private index / base image / `install-hook` for fleet rollout.

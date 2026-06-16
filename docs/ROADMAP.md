# Roadmap — individuals → teams → orgs (partner mindset)

> **Status:** the client (this `llm-spendguard` package) is production-ready and works standalone. The
> hosted team/org server (e.g. llmseg.ai) is a **separate repo, currently in development** — the URLs below
> describe where it will live, not a live service yet.

The guiding principle: **partner, not supervisor.** Every user sets their own limits and keeps their own
ledger. Teams and orgs get **visibility and pooled learnings by roll-up**, not control. Nobody's caps are
locked from above; the value is shared sight and shared knowledge, opt-in.

## Tiers

| Tier | Who | What they get | How |
|---|---|---|---|
| **I** (developer) | one person | local gate + caps **they** set, local ledger, local learnings | `pip install llm-spendguard` (this repo) — already shipped |
| **We** (team) | a team | see each other's usage, pooled + aggregated learnings, team rollup report | paste the **server key** (one key, server maps it to your team) + opt-in sync |
| **Us** (org) | an org | org-wide visibility, aggregate advice across teams, org rollup | members use keys the server maps to the org — no ids in client config |

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

### Client seam (BUILT — `saas.py`, `spendguard saas`)
The client is ready to connect the moment the server exists:
- **Config** lives in `~/.spendguard/saas.json` (gitignored; template `saas.example.json`): `enabled`, `url`
  (e.g. `https://api.llmseg.ai`), `api_key` (secret; or `SPENDGUARD_SAAS_KEY`), `visibility`
  (`private` | `team` | `org`), `sync_interval` (`off` | `hourly` | `daily` | `weekly`). Surfaced in
  `spendguard config` like every other knob.
- **One key = identity.** The client holds NO `team_id`/`org_id` — the SERVER maps the Bearer key to the
  user→team→org hierarchy. Less to leak, nothing to keep in sync.
- **Contract** (`saas.py`, versioned `{url}/v1`): `GET /v1/health`, `POST /v1/ledger` (per-day roll-up),
  `POST /v1/insights` (scrubbed abstracts), `GET /v1/insights?scope=` (pooled learnings). Bearer auth.
- **Cadence:** `sync_interval` drives when the roll-up pushes. `saas.sync(if_due=True)` is wired into the
  daily `report` cron (and `spendguard saas sync --if-due`), so it pushes on schedule and no-ops otherwise.
  `last_sync` tracked in `saas_state.json`.
- **Fail-safe by design:** every call degrades gracefully ("not connected") until the server is up; the
  client never depends on it. `visibility=private` = nothing leaves the machine. Reuses `share.py`'s scrub
  (abstracts only — never prompts/keys/$). Each user's SQLite ledger stays the source of truth.
- Status/test: `spendguard saas status` · `spendguard saas ping` · `saas sync [--if-due]` · `push` / `pull`.

## Access — slash commands (shipped)
`spendguard install-skills` deploys `/spend` (quick status: totals + leak + top learnings) and
`/spendguard-learn` (the advisor) into `~/.claude/skills/`, so they work as **slash-commands in Claude Code
(CLI + the VS Code extension)**. A native VS Code panel is possible later but optional — the skills cover
both surfaces today.

## Build order
1. **(done)** Client: gate / pricing / ledger / advisor / learnings / reconcile-ledger / report / slash-commands.
2. **(done)** Client **sync** seam — `saas.py` + `saas.json` config + `spendguard saas`; speaks the `/v1`
   contract, fail-safe until the server exists.
3. **`llm-spendguard-server`** (separate repo, NEXT): implement the `/v1` contract — account/team/org, ingest,
   aggregate visibility + learnings + advice, billing. llmseg.ai. The key-holding **proxy** (the only true
   no-bypass guarantee) is the natural first milestone here.
4. Publish client to PyPI; orgs use private index / base image / `install-hook` for fleet rollout.

---
name: spend
description: Quick spendguard status — current LLM spend (today/7d/month), the ledger-leak check, the top cost learnings, and the team/org roll-up (SaaS) connection. Use when the user asks "how much have we spent", "current LLM cost / token spend", "what are our cost learnings", or wants a fast spendguard readout. Read-only, no LLM spend.
---

# spend — quick spendguard status

Run these read-only commands (no LLM spend) and summarize for the user — lead with today's total, flag any
leak alert, then the top learnings:

```
spendguard report            # today / 7d / month totals + ledger-leak alert + top learnings
spendguard insights list     # all confidence-scored learnings
spendguard calls             # $/good-result per intent (if the call corpus is enabled)
```

Follow-ups the user may want:
- **Plan a new task:** `spendguard brief --task "<what they want to do>"` — pre-filled confirm/correct plan.
- **Per-intent recommendation:** `spendguard optimize --intent <X>` (caged, estimate-first — shows cost before spending).
- **Find ungoverned spend:** `spendguard reconcile-ledger`.

Keep the summary tight: $ today/7d/month, leak (if any), and the 3 most actionable learnings.

## Team / org roll-up (SaaS — opt-in)

If this repo is connected to a spendguard server (dashboard at https://llm-spendguard-server.vercel.app — note
the hosted server is in development), the local ledger rolls UP for team/org visibility. The client never
proxies tokens; only a scrubbed per-day roll-up leaves, at the configured `visibility`.

```
spendguard saas status         # url · key(set?) · contributor · project · visibility · sync state
spendguard saas reconcile      # make the LOCAL ledger reflect provider-billed TRUTH (the must-do before trusting $)
spendguard saas audit          # triple-check completeness: every batch accounted (complete=true / unaccounted=[])
spendguard saas push --dry     # PREVIEW the exact roll-up payload (no send, no spend)
spendguard saas push           # push the per-day roll-up now (idempotent)
spendguard saas commands       # run any server-queued work (reconcile + re-tag); reports a scrubbed result
spendguard saas sync --if-due  # reconcile + push + run queued work (cadence-safe; used by cron / the daily report)
spendguard resources show      # vast.ai GPU cost by project (each instance's LABEL → project)
spendguard resources sync      # push THIS repo's GPU spend → its org (provider=vastai, kind=gpu, multi-tag)
```

**Attribution model — every roll-up row is `(org/team × user × project)`:**
- **org/team** = the scope the **key** is bound to (mint keys in the server's *Keys* tab; one key per repo).
- **user** = `contributor` (set it to your **org email** so the server maps it to your member). Defaults to
  git `user.email`, then `$USER@host`.
- **project** = the WHAT (the repo/work). Defaults to the git repo name; set `project` per repo to be explicit.
- **`llmseg`** = spendguard's OWN meta spend (the advisor/learning calls) — always tagged separately and shown
  called-out on the dashboard.

**Per-repo connection** lives in a gitignored `.spendguard.json` at the repo root (overlays the global
`~/.spendguard/saas.json`; env wins). So different repos on one machine push to different orgs:
```json
{ "enabled": true, "url": "https://llm-spendguard-server.vercel.app",
  "api_key": "sg_team_…", "contributor": "you@org.com", "project": "your-repo", "visibility": "org" }
```
The push only sends rows for this connection's `project` (+ `llmseg`), so one machine's ledger never
cross-attributes to the wrong org.

# spendguard Data Plane — primed, distributed, collectively-improving data

**Status:** SPEC (draft 2026-09-30). Design of record for the shared data that makes spendguard useful on day one
and better with every user. Scoped to llm-spendguard (client) + llm-spendguard-server (server). Build follows the
**Sequencing** section; each increment is its own PR with its own guard.

## 0. Why (the moat)

A cost gate everyone runs *locally* is a commodity. A gate that gets *smarter because* everyone runs it is
defensible. The trigger for this spec: on 2026-09-30 a Gemini embedding run lost 5,760/5,768 inputs to a batch
ceiling of 100 that had to be *discovered by losing data*. It was measured once (`embed_ceiling_probe.py`) and is
now a catalog fact — **no other user should ever rediscover it.** That is the whole thesis: measure/curate once,
distribute to all; and for the things that vary by use, let the collective's measurements become each new user's
prior.

Non-negotiable framing (inherited from the product's privacy stance): **local-first, always.** Everything works
offline with the shipped floor; the server and the collective are opt-in accelerants, never a dependency, and never
see prompts, outputs, or keys.

## 1. The model: one data plane, three tiers

Each tier is a **fallback for the next** — a value is resolved from the richest tier available, degrading cleanly:

| Tier | Source | Reaches the user by | Offline? | Example |
|---|---|---|---|---|
| **T1 — Shipped floor** | curated data in the wheel | `pip install` | yes | `model_catalog.json` (prices, context, output + **embed ceilings**, reasoning floors, capabilities) |
| **T2 — Server-fresh** | server-hosted canonical layer | `spendguard sync-*` (pull) | last-pulled cache | a ceiling/price/model added *after* the last release |
| **T3 — Crowd-aggregated** | collective measurements, validated + aggregated server-side | `spendguard sync` (pull priors) + opt-in push | last-pulled cache | "for intent `code-review`, model X held quality at $Y (n=N users)" |

Resolution order at a call site is always **local override → T3 prior (if trusted) → T2 fresh → T1 floor → typed
absence**. A value never silently vanishes; when nothing is known it is an honest `None`/UNKNOWN, never a guess
(the existing catalog discipline).

## 2. Two data classes — the load-bearing distinction

Everything in the plane is exactly one of these, and they are **never mixed** (mixing them is how a crowd average
corrupts a published fact, or a hard verdict masquerades as a soft prior):

### Class A — CANONICAL FACTS
Prices, context windows, output ceilings, **embedding batch ceilings**, reasoning floors, capability flags, model
ids/aliases. **Sourced or measured ONCE, verified, provenance-stamped.** You do **not** crowd-average a provider's
published price; you verify it and ship it. The embed ceilings measured today are this class. Distribution: T1 ship
+ T2 server-refresh. The collective's role here is only to **flag drift** ("N users now see a 400 at the old
ceiling") → triggers a *re-verification*, never an automatic overwrite.

### Class B — EMPIRICAL PRIORS
Cost×quality per intent, cheapest-effort-that-holds per (intent, model), lane reliability, realized savings,
"which model wins for intent X". These **genuinely improve with N users.** Aggregated server-side with **consensus +
confidence + sample size**, distributed as T3 priors, and **always re-validatable locally** — a prior seeds the
advisor, it does not override a local measurement. This is the existing advisor `insights` concept (intent / lesson /
evidence / confidence / lifecycle), scaled from one user to the collective.

**The test for which class a datum is:** *could two honest, careful users measure it and disagree?* If no (a 400 at
N+1 is a 400 for everyone) → Class A, verify once. If yes (quality rides your rubric + data) → Class B, aggregate as
a confidence-weighted prior.

## 3. Privacy & sharing model

The hard floor, unchanged: **prompts, outputs, and API keys NEVER leave the machine.** The roll-up already sends
only per-day/per-model aggregates.

**Intents are SHAREABLE by design** (product decision, 2026-09-30) — they are the *aggregation key* that makes the
Class-B flywheel work (a prior is only useful keyed to "what kind of job"). This is a deliberate contract with one
obligation it puts on the user, which the tool must make impossible to violate silently:

- **An intent label is a JOB-TYPE, never an identity.** `code-review`, `loinc-typing`, `invoice-extract` — never a
  client, project, person, or matter name. Intents are collective metadata.
- **Guard (new):** a write-time + push-time **intent-label check** (agentic, not regex — it's a meaning judgement)
  that flags an intent label which reads like a proper noun / project / client / PII and refuses to share it (it
  still works locally, just isn't pushed), with a one-line nudge to rename. This makes "intents are shareable" safe
  without asking the user to self-police every label. (Local-only intents remain possible via an opt-out prefix,
  e.g. a leading `_`, for anyone who wants a private label.)

Everything shared is **scrubbed of free text**: an insight carries the intent label + model + measured numbers +
confidence + provenance, never the sample content that produced it.

## 4. Trust & validation — tiered by class

- **Class A** changes require **independent verification**, never a single report. A crowd "drift" signal (K
  independent users seeing the old ceiling/price fail) opens a re-verification task; the canonical value changes only
  when re-measured/re-sourced. Bias **conservative** where the failure is asymmetric — a too-HIGH embed ceiling 400s
  people (bad), a too-LOW one just costs extra requests (cheap) → when uncertain, publish the lower bound.
- **Class B** aggregates with **consensus + confidence + sample size + recency decay**, and every prior is a *seed*
  the local install re-validates before trusting for spend decisions (the existing lifecycle: insights are
  conditional rules re-validated as data grows). Outlier rejection on submission; a single user cannot move a prior.
- **Provenance is mandatory** on every datum: `source` (provider-doc / measured / crowd-aggregate), `as_of`,
  `verified` (bool/null), and for Class B `n` (contributors) + `confidence`.

## 5. Staleness, provenance & conflict resolution

The server is the **reconciler**. Ordering rules when two data points disagree:
1. **measured/verified > claimed/synced** (a probe beats a litellm-breadth guess).
2. **newer `as_of` > older** for the same source rank.
3. **consensus > singleton** for Class B (n weighted).
4. Class A conflict never auto-resolves to a crowd value — it queues a re-verification and holds the last verified
   value meanwhile.

Every distributed artifact carries an `as_of` and a content hash; clients cache with that stamp and show it
(`spendguard catalog --as-of`), so "the number is stale" is visible, never silent. Offline = serve the last cache +
the T1 floor, and say so.

## 6. Client contract (llm-spendguard)

Extends existing seams — **no new mechanism where one exists.**

- **Pull canonical (T2):** `spendguard sync-catalog` (EXISTS — today pulls litellm breadth) gains a source: the
  server's canonical catalog layer, written to `~/.spendguard/catalog_synced.json`, layered **above** the shipped
  `model_catalog.json` floor and **below** any local override. `model_catalog._load_records()` becomes
  floor→synced→local merge (today it reads only the shipped file).
- **Pull priors (T3):** `spendguard sync` (EXISTS via `saas sync`) also pulls Class-B priors into the advisor
  `insights` store, tagged `source=crowd`, `n`, `confidence`, `as_of` — read by `advise` / `best-value` as a seed,
  re-validated locally before it governs spend.
- **Push (opt-in):** on `saas push`/`sync`, send scrubbed Class-A drift signals + Class-B measurements (intent +
  model + numbers + provenance). Gated by the intent-label check (§3).
- **Everything degrades:** no server / no account → T1 floor only, identical to today. No behavior change for a
  purely-local user except a richer shipped floor.

## 7. Server contract (llm-spendguard-server)

New/extended endpoints (Bearer auth, the existing account anchor):
- `GET /v1/catalog?since=<as_of>` → the canonical Class-A layer (delta since a stamp), content-hashed. Read-only,
  cacheable, no auth required for the *public canonical* layer (it's shippable data); auth only gates org overrides.
- `POST /v1/measurements` → scrubbed Class-A drift signals + Class-B measurements. Validated, outlier-rejected,
  stored with contributor attribution (for consensus counting, not exposure).
- `GET /v1/priors?intent=<label>&scope=<self|team|global>` → aggregated Class-B priors (EXTENDS the roadmap's
  `/v1/insights` pooled pull). Aggregation is server-side: consensus + confidence + recency decay.
- **Aggregation job** (server-side, periodic): fold new measurements into the canonical layer (Class A, via a
  re-verification gate) and the priors (Class B, via the consensus model). This is where "gets better with more
  users" physically happens.

Server storage: the existing system-of-record DB gains a `measurements` table (raw, contributor-attributed) and
`priors` (aggregated, public-readable). The canonical catalog layer is generated from verified measurements + the
curated seed — the server-side analogue of `gen_prices_json.py`.

## 7b. Existing seams (grounded 2026-09-30) — EXTEND, not rebuild

Much of the plane is already scaffolded (CLAUDE.md #0). The build is extend-and-wire against these:

### Catalog (T2 · item 2)
- **Server:** `GET /v1/models` (Bearer, **GLOBAL** — migration 0008, no org scoping), backed by `lib/catalog.ts`
  (`refreshModelCatalog` pulls LiteLLM · `getModelCatalog` reads), refreshed daily by `cron/catalog-refresh`.
  Columns: model_id, provider, mode, max_input_tokens, max_output_tokens, price, capabilities, source, fetched_at.
- **Client:** already consumes it — `saas.fetch_models()` (GET /v1/models) + `sync_capabilities._fetch_server_catalog`
  / `_litellm_record` merge the server catalog **first** into the local LiteLLM cache, for CAPABILITIES.
- **THE GAP:** the server catalog is a **LiteLLM mirror** — it carries NO spendguard-CURATED data (`embed_max_batch`,
  verified prices, output/reasoning floors, provider_base). So the curated layer — the actual value, including today's
  embed ceilings — does **not** flow server→client; it reaches pip users only via the shipped `model_catalog.json`
  floor (0.11.4), i.e. only on a release.
- **Extension (item 2):** `refreshModelCatalog` also ingests the published curated `model_catalog.json` (from a
  canonical URL) with `source='spendguard-curated'` + the extra columns; `/v1/models` serves them; the client routes
  the server-fresh curated fields into `model_catalog` resolution (`embed_batch_ceiling`, …) as the T2 layer above the
  shipped floor.

### Priors (T3 · item 3)
- **Server:** `POST/GET /v1/insights` already push/pull SCRUBBED learnings (`spendguard.shared.v1`:
  task_class/regime/output_shape/scale/condition/action/mechanism/lesson/confidence/quality_basis/source/support;
  idempotent upsert on (scope_id, fingerprint); pgvector semantic search). `lib/learnings.ts` · `lib/core.ts`.
- **THE GAP:** insights are **ORG-SCOPED** (RLS on org_id). The product decision is a **GLOBAL opt-in pool.**
- **Extension (item 3):** a global scope for opted-in insights + global aggregation (consensus + confidence +
  recency) + `GET /v1/insights?scope=global`; the client opts in to push into / pull from the global pool.

### Taxonomy + signals
- `/v1/taxonomy` is the org→team×project **attribution** taxonomy (spend classification), **not** intent clustering —
  item 4's INTENT taxonomy is a distinct, new concept. `/v1/signal` exists as a measurement/signal seam (to confirm)
  usable for Class-A drift signals.

## 8. Invariants / guards (un-regressable — each ships with its enforcement)

1. **The floor always ships and is complete enough to run.** `tests/test_packaging_data_files.py` (EXISTS as of
   0.11.4) — every runtime data file is in `package-data`.
2. **Tiers degrade, never fail.** A test asserts resolution with T3/T2 absent falls to T1 then typed-UNKNOWN, no
   exception, no guess.
3. **Class A is never crowd-averaged.** A guard asserts the canonical-write path accepts only verified/measured
   provenance, never a raw crowd value.
4. **No free text leaves.** A push-path test asserts the payload carries only labels + numbers + provenance — never
   sample content — and that the intent-label check runs before any push.
5. **Provenance is total.** Every distributed datum has `source` + `as_of` (+ `n`/`confidence` for Class B); a test
   rejects a datum missing it.
6. **Conservative on asymmetric facts.** A test pins that an unverified embed-ceiling drift lowers (never raises) the
   published ceiling until re-verified.

## 9. Sequencing (the increments) — grounded to the real seams (§7b)

Build order (user, 2026-09-30): **tighten spec (this) → client overlay → full build**.

1. **[DONE] Ship the floor.** `model_catalog.json` in the wheel (0.11.4).
2. **Curated catalog flows server→client (T2 · item 2).**
   - *Client overlay (safe, first):* `model_catalog` resolution layers a server-synced catalog above the shipped floor
     (**floor→synced→local**), fed by the EXISTING `/v1/models` pull — zero-risk, and the wiring the next step fills.
   - *Server curated ingest:* `refreshModelCatalog` also ingests the curated `model_catalog.json`; a migration adds the
     curated columns (`embed_max_batch`, verified price/ceiling/reasoning, provider_base, `source`); `/v1/models`
     serves them. → a new embed ceiling reaches pip users on the daily refresh, **no release**.
3. **Global-opt-in priors (T3 · item 3).** Extend `/v1/insights` with a global scope + aggregation (consensus +
   confidence + recency decay); client opts in to push/pull global priors into the advisor `insights` store, read as a
   re-validatable seed. Start with cost×quality-per-intent + wild-measured ceilings (Class-A drift → re-verify).
4. **Intent-label safety + intent taxonomy.** The agentic intent-label check (§3) + a shared intent taxonomy (emergent
   clustering + curated spine), distinct from the existing org/team/project attribution taxonomy.

## 10. Open questions (flag before building each stage)

- Canonical `/v1/catalog` public (unauth) vs account-gated — leaning public for the shippable layer, auth only for
  org overrides. Confirm before wiring.
- Team vs global prior scoping default (private team pool vs global) — leaning global-opt-in with a team layer above.
- Taxonomy: fixed seed list vs embedding-clustered emergent intents — leaning emergent (cluster) with a curated
  spine, so a novel intent still aggregates.

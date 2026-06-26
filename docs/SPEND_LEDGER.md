# SpendLedger — Schema & Interface (review)

The single in-process gateway to spend data (a context/data provider; MCP-style, **not** a server). **No consumer
writes raw SQL** — the class owns the schema and all queries/joins, returns dicts, and routes the agentic
*attribution* through one path. Deterministic SQL for queries; the LLM is used **only** for attribution (meaning),
**recorded** so re-runs read it (repeatable).

**Built to financial-systems standards (Xero / QuickBooks-style — flexibility with controls):**
- **Money = integer micro-USD** (`*_micros`, ×1e6) — never float; sums are exact.
- **Time = UTC canonical** (`ts_utc`) + source-local (`tz`/`local_datetime`); accounting `day`/`period` derived in the
  **reporting tz** (`SPENDGUARD_REPORTING_TZ`); **transaction date** (`occurred_at`) ≠ **posting date** (`recorded_at`).
- **Multi-pass enrichment with controls** — a spend event is **mutable across passes** (ingest → attribute → reconcile)
  until its period is **locked** (Xero *lock date* / QuickBooks *close the books*). Once locked it's immutable;
  corrections are **adjusting / reversing entries**, never edits. Lifecycle: `status` draft → posted → reconciled → locked.
- **The audit trail is the immutable record** — every change is appended to **`spend_audit`** (who · when · field ·
  old→new · pass), which is append-only + **hash-chained** (`verify_audit_chain()`). Integrity lives in the *log*, not
  the live row — so enrichment stays flexible while every change stays provable.
- **Self-contained record + link-ids** — snapshots cost/attribution/rates + `seg_id`/`call_id`/`conv_id`/`batch_id`/`model`.

**Status:** v4 schema + **lifecycle/audit built + validated** (**22/22** tests, `tests/test_spend_ledger.py`):
record/update/lock_period/reverse/adjust/history + `spend_audit` hash chain. Attribution engine (Step 3) + migration +
consumer hookup planned. File: `src/spendguard/ledger.py`. DB: `~/.spendguard/spend.db`.

---

## 1. `spend_events` — the forensic schema

**One row per spend EVENT.** The four cost types are **separate columns** so a rollup is `SUM(col)` — never a leaky
`GROUP BY kind`. Identity + lineage + the attribution audit make every dollar explainable.

### Identity / dedup
| column | type | purpose |
|---|---|---|
| `id` | TEXT PK | deterministic evidence hash — re-recording the same event is a no-op (**kills double-count**) |
| `dedup_key` | TEXT | natural key (message-id / batch+custom_id); else derived from the evidence signature |
| `source` | TEXT | `gate` \| `reconstruction` \| `batch-api` \| `gpu` \| `est-chat` |
| `content_hash` | TEXT | content fingerprint (from `seg_attribution`) |

### Time — UTC canonical; accounting day/period in the reporting tz
| column | type | purpose |
|---|---|---|
| `ts_utc` | TEXT | canonical **UTC**, tz-aware ISO-8601 — ordering + math |
| `occurred_at` | TEXT | **transaction date** — when the spend happened (UTC) |
| `recorded_at` | TEXT | **posting date** — when we booked it (UTC) |
| `tz`, `local_datetime` | TEXT | source zone + wall-clock (date-boundary context) |
| `day`, `period` | TEXT | accounting day (YYYY-MM-DD) / period (YYYY-MM) in the **reporting tz** |
| `eligibility_window`, `window_start/end` | TEXT | period eligibility + reconstructed-run range |

### Cost — integer **micro-USD**, separate columns (the core fix)
| column | type | purpose |
|---|---|---|
| `batch_micros` | INT | Batch-API spend (micro-USD) |
| `realtime_micros` | INT | realtime/per-item |
| `est_chat_micros` | INT | Claude Code/Codex plan usage — the **est-value** axis (`billed=0`) |
| `remote_compute_micros` | INT | vast.ai GPU |
| `subscription_micros` | INT | flat plan fee (Max/Pro), attributed proportionally |
| `currency` | TEXT | default `USD` (+ `fx_rate`/`base_micros` for multi-currency) |
| `cost_type` | TEXT | `batch`\|`realtime`\|`est_chat`\|`remote_compute`\|`subscription` — filled as applicable |
| `billed` | INT | 1 real $; 0 est_chat |
| `is_meta` | INT | spendguard's OWN spend — **excluded from workload rollups** |
| `cost_basis` | TEXT | `printed`\|`estimated`\|`gate-measured`\|`provider-reconciled` (**forensic confidence**) |
| `amount_confidence` | REAL | 0.0–1.0 |
| `rate_in`, `rate_out` | REAL | $/1M-tokens snapshot from `pricing.price(model)` → `cost = tokens × rate` auditable |

> **Money is integer micros — never float** (summing thousands of sub-cent amounts is exact). `billed = batch +
> realtime + remote_compute + subscription`; `est_value = est_chat` — never summed. `rollup` returns exact micros +
> a `*_usd` display value.

### Lifecycle · reconciliation · provenance · billing  *(integrity → `spend_audit`, §1d)*
| group | columns |
|---|---|
| **lifecycle** (mutable until locked) | `status` (draft\|posted\|reconciled\|locked\|reversed\|void), `revision`, `locked`, `locked_at`, `lock_reason`, `reverses_id`, `adjusts_id`, `superseded_by` |
| **reconciliation/close** | `reconciled`, `reconciled_vs`, `reconciled_at`, `reconciliation_id`, `gap_flag`, `period_closed` |
| **provenance** | `recorded_by`, `ingest_version`, `schema_version`, `evidence_uri` |
| **billing / multi-entity** | `account_id`, `customer_id`, `cost_center`, `engagement`, `billable`, `invoice_id` |

> The live row carries **no** `row_hash` — it's mutable across passes. Integrity is the append-only **`spend_audit`**
> log (§1d), which *is* hash-chained.

### Provider / model
| column | type | purpose |
|---|---|---|
| `provider`, `model` | TEXT | e.g. `openai` / `gpt-5.5` |
| `model_kind` | TEXT | `completion` \| `embedding` \| `image` \| `gpu` — **structurally prevents embeddings-priced-as-completions** |
| `finish` | TEXT | stop reason |

### Metering
| column | type | purpose |
|---|---|---|
| `in_tok`, `out_tok` | INT | tokens |
| `cache_read_tok`, `cache_write_tok`, `reasoning_tok` | INT | caching / reasoning tokens (affect cost) |
| `num_calls` | INT | calls in a loop (1 = single) |
| `num_items` | INT | work scale (items processed) |
| `latency` | REAL | seconds |

### Attribution result
| column | type | purpose |
|---|---|---|
| `org`, `team` | TEXT | the org / team |
| `projects` | TEXT(JSON) | **multi-project** array |
| `project_primary` | TEXT | the dominant project |
| `member_ref` | TEXT | who (saas identity) |

### Lineage / evidence (the forensic trail)
| column | type | purpose |
|---|---|---|
| `conv_id`, `seg_id`, `cwd` | TEXT | session / subconversation segment / working dir (**the deterministic anchor**) |
| `batch_id` | TEXT | provider batch id |
| `from_message_ids`, `prior_message_ids`, `post_message_ids` | TEXT(JSON) | the messages that evidence + bracket the spend |
| `script`, `repo`, `host` | TEXT | what ran it / which repo / local-or-vast-box |
| `prompt_hash`, `prompt_snip`, `output_snip` | TEXT | dedup / sample |

### Attribution audit (why · what · how)
| column | type | purpose |
|---|---|---|
| `attr_what` | TEXT | what the spend was (the work) |
| `attr_why` | TEXT | why this org/project (reasoning) |
| `attr_how` | TEXT | `cwd-match` \| `lineage` \| `llm` \| `batch-map` \| `gate-inline` |
| `attr_reason` | TEXT | the LLM's verbatim reason |
| `attr_confidence` | INT | 0–100 |
| `attr_source`, `attr_model` | TEXT | which engine / which LLM decided |
| `attr_ts`, `attr_version` | TEXT | when / engine+prompt version (**repeatability audit** — knows if a determination is stale) |

### Reconciliation / lifecycle
| column | type | purpose |
|---|---|---|
| `reconciled` | INT | cross-checked vs provider truth? |
| `reconciled_vs` | TEXT | `provider` \| `admin-dev-xcheck` |
| `gap_flag` | TEXT | Σ doesn't reconcile → flagged (never silently dropped) |
| `superseded_by`, `recon_marker` | TEXT | idempotent rebuild |

### Quality / governance / free
| column | type | purpose |
|---|---|---|
| `quality`, `quality_src`, `quality_conf` | TEXT/INT | judged quality |
| `cache_hit` | INT | served from cache |
| `savings_cv` | REAL | counterfactual savings |
| `tags` | TEXT(JSON) | free-field array |

**Indexes:** `org, day, conv_id, source, batch_id, dedup_key, reconciled, model_kind`.

---

## 1b. Table relationships — denormalized record + link-ids

`spend_events` is the **one canonical ledger** — a self-contained financial record that **snapshots** cost + attribution
+ rates, **mutable across passes while open and immutable once locked** (§1c), with every change logged to `spend_audit`
(§1d). It also carries **link-ids** to the source evidence for drill-down:

| link-id | → table | role |
|---|---|---|
| `seg_id` | `seg_attribution` | the cwd-anchored attribution **determination** (cache, reused across events) → snapshots into `attr_*` |
| `call_id` | `calls` | the gate's per-call record (raw capture that **feeds** spend_events) |
| `conv_id` | transcript | the session |
| `batch_id` | provider batch | the Batch-API job |
| `model` | `model_facts` | the **price book** → `rate_in/out` snapshot from it |

`seg_attribution` = attribution **cache** · `model_facts` = **price book** · `calls` = raw gate capture (feeds) ·
`charges` = the old ledger → **migrated + retired**. So: "all in one row" (self-contained) **plus** link-ids (traceable
to evidence) — the standard accounting shape (journal entries referencing source documents).

---

## 1c. Lifecycle & controls — the Xero / Intuit model

A spend event is **enriched across passes**, not written once: **mutable while open, immutable once locked.**

**States** (`status`): `draft → posted → reconciled → locked`; plus `reversed` / `void`.
- **draft** — ingested (gate / batch-api / reconstruction); cost present, attribution may be pending.
- **posted** — attribution pass done (`org`/`team`/`projects` + `attr_*`).
- **reconciled** — cross-checked vs provider truth (`reconciled`, `reconciliation_id`).
- **locked** — its period is closed (`lock_date`) or `status=locked` → **immutable**.

**Controls (the "appropriate controls"):**
- Every pass **UPDATEs** the row **and appends to `spend_audit`** — no silent change.
- **Lock** = a per-period `lock_date` (close the month) **or** row `status=locked`. `record`/`update` **refuse** to
  modify a row that is locked or whose `period ≤ lock_date`.
- **Corrections after lock** = **reverse** (a new row negating the original, `reverses_id`) and/or **adjust** (a new
  corrected row, `adjusts_id`). The locked row is never touched — exactly like a posted journal entry.
- Optional **period seal** at close (a hash over the period's final rows) as an extra anchor.

## 1d. `spend_audit` — the append-only, hash-chained change log

The immutable forensic record: one row per change to a spend event. **Never edited or deleted.**

| column | purpose |
|---|---|
| `id` | PK |
| `event_id` | → `spend_events.id` |
| `ts` | UTC of the change |
| `actor` | who/what (gate, attribution-v2, reconcile-run, user) |
| `pass` | ingest \| attribute \| reconcile \| adjust \| lock \| reverse |
| `field`, `old_value`, `new_value` | the change (one row per field) |
| `reason` | why |
| `prev_hash`, `row_hash` | **hash chain** — `verify_audit_chain()` proves the log was not altered |

Integrity lives **here** (append-only), so the live `spend_events` row can be freely enriched while every change stays
provable. `history(event_id)` returns a row's full change timeline.

---

## 2. `SpendLedger` — the interface

```python
SpendLedger(db_path=None)          # opens the canonical db (config.db_path()), ensures schema
```

### Built (Steps 1–2)

| method | returns | notes |
|---|---|---|
| `record(ev) -> id` | event id | validated write; `(kind, usd)` routes to the right cost column; deterministic id → **dedup** |
| `get(eid) -> dict\|None` | event dict | JSON columns deserialised |
| `query(since=, until=, where=, limit=) -> list[dict]` | events | `where` = exact-match column filters; `since/until` filter `day` |
| `rollup(group_by=, since=, until=, where=, include_meta=) -> dict` | breakdown | exact micros + `*_usd`; `billed` vs `est_value`; meta excluded by default |
| `by_repo(repo, since=, until=) -> dict` | breakdown | repo-scoped — **charm = $0 remote** (a filter, can't leak) |

```python
# record (a reconstructed realtime run)
led.record({"source":"reconstruction","kind":"realtime","usd":220.0,
            "provider":"openai","model":"gpt-5.5","model_kind":"completion","cost_basis":"printed",
            "org":"Healiom","team":"lmm","projects":["lmm"],"cwd":"~/Documents/claude/lmm","repo":"lmm",
            "attr_what":"loinc stem pass","attr_why":"cwd=lmm","attr_how":"cwd-match"})

led.rollup()       # {<cost>_micros, <cost>_usd, billed_micros, billed_usd, est_value_micros, est_value_usd, n}
led.rollup("org")                  # {"Healiom": {...}, "Ensight": {...}}  (is_meta excluded unless include_meta=True)
led.by_repo("charm")               # {... remote_compute_usd: 0.0, billed_usd: 26.0 ...}
```

### Planned (Steps 3–5)

| method | role |
|---|---|
| `update(id, changes, actor, reason)` | mutate an OPEN row; **refuses if locked / period ≤ lock_date**; logs every field to `spend_audit` |
| `attribute(id, …)` | the **one** agentic pass: per-segment, cwd-anchored, temp=0, `seg_attribution` join, writes `attr_*`; **convergence loop** (classify → cross-check Σ-per-org vs provider truth → re-attribute uncertain → until stable). Update + log. |
| `reconcile(id, …)` | cross-check vs provider truth; set `reconciled`/`reconciliation_id`/`gap_flag`. Update + log. |
| `lock_period(period, reason)` | close a period → its rows become immutable; optional period **seal** (hash over final rows) |
| `reverse(id, …)` / `adjust(id, …)` | post-lock corrections — new rows (`reverses_id`/`adjusts_id`); the locked row is never touched |
| `history(id) -> list` | the row's full change timeline (from `spend_audit`) |
| `verify_audit_chain() -> (ok, bad_id)` | recompute the `spend_audit` hash chain — proves the log wasn't altered |
| `export(scope)` | dashboard payload (consumers call this, never SQL) |

---

## 3. Design invariants (what the tests enforce)

1. **Separate cost columns, never mixed** — a row's cost is in exactly one (`cost_type` labels which); rollups `SUM(col)`.
2. **billed ≠ est_value** — `billed = batch + realtime + remote_compute + subscription`; `est_chat` separate, never summed; `is_meta` rows excluded from workload rollups.
3. **Dedup by deterministic id** — same evidence records once (no double-count).
4. **Per-repo scope is a filter** — charm shows only charm; remote can't leak across repos.
5. **`model_kind` + `cost_basis`** — embeddings can't be priced as completions; printed-vs-estimate is explicit.
6. **Attribution is recorded with its reasoning** — every $ has `attr_what/why/how`, and a re-run reads the
   determination (deterministic) rather than re-asking the LLM.
7. **No raw SQL outside `SpendLedger`** (to be enforced by a CI guard in Step 5).
8. **Money is integer micros — exact** — `rollup` sums micros (no float drift); `*_usd` is a display conversion.
9. **Time is UTC; transaction ≠ posting** — `occurred_at` drives the accounting `day`/`period` (reporting tz); `recorded_at` is the booking time.
10. **Mutable until locked** — a row is enriched across passes (`update`/`attribute`/`reconcile`); `record`/`update` **refuse** a row that is `locked` or whose `period ≤ lock_date`.
11. **Corrections after lock = adjusting entries** — never edit a locked row; post a `reverse`/`adjust` (new row, `reverses_id`/`adjusts_id`).
12. **Audit trail is the immutable record** — every change appends to `spend_audit`; that log is **hash-chained** and `verify_audit_chain()` proves it wasn't altered.

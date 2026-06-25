# SpendLedger — Schema & Interface (review)

The single in-process gateway to spend data (a context/data provider; MCP-style, **not** a server). **No consumer
writes raw SQL** — the class owns the schema and all queries/joins, returns dicts (JSON columns deserialised), and
routes the agentic *attribution* through one path. Deterministic SQL for queries; the LLM is used **only** for
attribution (meaning), and that determination is **recorded** so re-runs read it (repeatable, not re-guessed).

**Status:** Steps 1–2 built + validated (12/12 tests, `tests/test_spend_ledger.py`). Steps 3–5 planned.
File: `src/spendguard/ledger.py`. DB: `~/.spendguard/spend.db` (shared with `seg_attribution`, `calls`, `charges`).

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

### Time
| column | type | purpose |
|---|---|---|
| `ts`, `day` | TEXT | event timestamp / date |
| `window_start`, `window_end` | TEXT | range for a reconstructed run that spans time |
| `eligibility_window` | TEXT | the period this spend is eligible to (**per-period split**) |

### Cost — separate columns (the core fix)
| column | type | purpose |
|---|---|---|
| `batch_usd` | REAL | Batch-API spend |
| `realtime_usd` | REAL | realtime/per-item spend |
| `est_chat_usd` | REAL | Claude Code/Codex plan usage (`billed=0`) — the **est-value** axis |
| `remote_compute_usd` | REAL | remote compute (vast.ai GPU) |
| `subscription_usd` | REAL | flat plan fee (Max/Pro), attributed proportionally by plan-usage |
| `cost_type` | TEXT | `batch`\|`realtime`\|`est_chat`\|`remote_compute`\|`subscription` — categorical label, **filled as applicable** |
| `billed` | INT | 1 for real $; 0 for est_chat |
| `is_meta` | INT | spendguard's OWN spend (usage-pulls/reconstruction) — **excluded from workload rollups** |
| `cost_basis` | TEXT | `printed` \| `estimated` \| `gate-measured` \| `provider-reconciled` (**forensic confidence**) |
| `amount_confidence` | REAL | 0.0–1.0 |
| `rate_in`, `rate_out` | REAL | $/1M-tokens **snapshotted from `pricing.price(model)`** (realtime `in_`/`out` vs `batch_in`/`batch_out`) → `cost = tokens × rate` is auditable |

> **A row carries its cost in exactly one column.** `billed = batch + realtime + remote_compute + subscription`
> (REAL $); `est_value = est_chat` — the two axes are **never summed**. (`rollup` sums every `COST_COL` generically,
> so adding a column can't drift the total.)

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

`spend_events` is the **one canonical ledger** — an immutable, self-contained financial record that **snapshots** the
determined cost + attribution + rates at record time (re-attribution **supersedes**, never mutates history). It also
carries **link-ids** to the source evidence for drill-down:

| link-id | → table | role |
|---|---|---|
| `seg_id` | `seg_attribution` | the cwd-anchored attribution **determination** (cache, reused across events) → snapshots into `attr_*` |
| `call_id` | `calls` | the gate's per-call record (raw capture that **feeds** spend_events) |
| `conv_id` | transcript | the session |
| `batch_id` | provider batch | the Batch-API job |
| `model` | `model_facts` | the **price book** → `rate_in/out` snapshot from it |

`seg_attribution` = attribution **cache** · `model_facts` = **price book** · `calls` = raw gate capture (feeds) ·
`charges` = the old ledger → **migrated + retired**. So: "all in one row" (self-contained, immutable) **plus**
link-ids (traceable to evidence) — the standard accounting shape (journal entries referencing source documents).

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
| `rollup(group_by=, since=, until=, where=) -> dict` | breakdown | the 4-column split + `billed` + `est_value`; `group_by=None` → totals; `"org"` → per-org |
| `by_repo(repo, since=, until=) -> dict` | breakdown | repo-scoped rollup — **charm = $0 remote** (a filter, can't leak) |

```python
# record (a reconstructed realtime run)
led.record({"source":"reconstruction","kind":"realtime","usd":220.0,
            "provider":"openai","model":"gpt-5.5","model_kind":"completion","cost_basis":"printed",
            "org":"Healiom","team":"lmm","projects":["lmm"],"cwd":"~/Documents/claude/lmm","repo":"lmm",
            "attr_what":"loinc stem pass","attr_why":"cwd=lmm","attr_how":"cwd-match"})

led.rollup()       # {batch_usd, realtime_usd, est_chat_usd, remote_compute_usd, subscription_usd, billed, est_value, n}
led.rollup("org")                  # {"Healiom": {...}, "Ensight": {...}}
led.by_repo("charm")               # {... remote_compute_usd: 0.0, billed: 26.0 ...}
```

### Planned (Steps 3–5)

| method | role |
|---|---|
| `attribute(event)` | the **one** agentic path: per-segment, cwd-anchored, temp=0, joins `seg_attribution`, writes `attr_*`; **convergence loop** (classify → cross-check Σ-per-org vs provider truth → re-attribute only uncertain → until stable). `record()` routes through it. |
| `reattribute(filter)` | re-run attribution over a slice (e.g. fix lmm-in-Ensight) |
| `reconcile(provider_truth)` | set `reconciled`/`gap_flag`; mark Σ-per-org vs truth |
| `clear(marker)` / `supersede(...)` | idempotent rebuild |
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

# llm-spendguard — Domain Taxonomy (authoritative)

**Status:** finalized 2026-11-02 on branch `spec/domain-taxonomy-seed`.
**Supersedes:** the 10-domain hypothesis in [`DOMAIN_TAXONOMY.seed.md`](DOMAIN_TAXONOMY.seed.md) (kept unedited as the pre-registered hypothesis).
**Derived from:** an independent, coverage-verified re-derivation over all 220 `src/spendguard/*.py` modules
([`DOMAIN_TAXONOMY.discovery_run1.json`](DOMAIN_TAXONOMY.discovery_run1.json)); the seed↔discovery diff is in
[`DOMAIN_TAXONOMY.discovery_run1.md`](DOMAIN_TAXONOMY.discovery_run1.md).

## 0. How to read this

The structure is **two levels**:

1. **Capability kind** (7) — the top-level grouping and the reorganization unit. A module's kind answers *what it does
   to money/tokens/work*, not *what feature it belongs to*.
2. **Bounded context** (21) — the domain a module lives in, nested under its kind.

```
STORE (5)            GENERATE (3)         ACTIVELY-MANAGE (3)   ANALYZE (7)                 EGRESS (1)   PLATFORM (1)  INTERFACE (1)
├ Reference Data     ├ Spend Capture      ├ Admission & Enf.    ├ Cost Estimation          └ Federation └ Platform    └ Surfaces
├ Configuration      ├ Execution          ├ Routing            ├ Treasury / Valuation         & Egress   (Install/
├ Money Ledger       └ Work Integrity     └ Token Operations   ├ Measurement & Exp.                      Fleet/
├ Spend Search                                                 ├ Learning & Advisory                     Release)
└ Call Corpus                                                  ├ Reconciliation & Attr.
                                                               ├ Spend Reporting
                                                               └ Assurance & Self-Verify
```

Two capability kinds were added beyond the seed's five because real code fit none of them:
- **EGRESS** — code that sends data off the machine *and owns the scrubbing/projection* of what leaves. Not a thin
  interface (it makes de-identification and shaping decisions), not pure analysis (it emits).
- **PLATFORM** — the product operating *itself*: version SSOT, release pointer, distribution attestation, fleet
  convergence, scheduler, install/interview, interpreter gating. None of GENERATE/MANAGE/STORE/ANALYZE/INTERFACE
  describes a package installing and running its own machinery.

---

## 0b. Access surfaces — how domains touch each other (the decoupling contract)

**Every bounded context publishes exactly ONE public surface — a "port" / published language — and other contexts
access it ONLY through that surface, never by importing its internal modules.** This is the third structural element
(after *kind* and *context*): each context declares the operations its peers may call, and everything else is private.

- **Cross-context imports go only to the target context's declared surface.** A module in context A importing a module
  in context B is legal only if B's module *is* B's surface. Reaching into B's internals is a boundary violation.
- **Intra-context imports are unconstrained.** Cohesion *inside* a context is expected; the constraint is purely on
  what crosses a context boundary. So a context may stay internally interconnected while the graph *between* contexts
  stays small and inspectable.
- **This is the actual cure for the §3 mesh.** The 248 same-layer cyclic edges are overwhelmingly deep cross-context
  imports; routing each through the target's surface turns an N×N internal mesh into N context→surface edges and stops
  a context's internals from binding every peer. The §3 magnitude ratchet then *proves* the SCC shrinks as surfaces
  land.
- **Enforcement (extends `architectural_fitness`):** a cross-context edge whose target is not the declared surface is
  gated red. Mechanism: a `context → surface-module` registry (data), checked against the real import graph — the same
  Tarjan/edge machinery the ratchet uses.
- **Not the same as the Surfaces context (#21).** #21 is the *user*-facing facade (cli/mcp/api/http). The access
  surface here is the *inter-domain* facade each of the 21 contexts publishes to its sibling contexts. A context's
  surface is typically its package `__init__` or one named facade module; naming all 21 is the first step of the cure
  (§3). This mirrors the global **capability-SSOT** doctrine — declare once, access through the published surface.

---

## 1. The 21 bounded contexts

Layer tags (`L0`–`L4`) are the intended dependency layer, lowest first; see §3 for the acyclicity contract.

### STORE — systems of record and the facts routing reads

#### 1. Reference Data: Pricing & Model Catalog · `STORE` · L0
Denominations and exchange rates: canonical prices, authored per-model facts and capabilities, lane/registry SSOTs,
money scale, and the sync/refresh paths that author them.
`pricing.py` · `catalog_price_authoring.py` · `model_catalog.py` · `catalog.py` · `lane_registry.py` ·
`lane_catalog.py` · `reasoning_equivalence.py` · `money_scale.py` · `sync.py` · `sync_capabilities.py` · `refresh.py`

#### 2. Configuration · `STORE` · L0 (target; `config.py` is NOT L0 today — see note)
Declarative settings registry and resolved runtime config (paths, knobs, key loading).
`config_schema.py` · (+ the config half of `config.py` — see §2 splits)

> **Measured 2026-11-02 — `config.py` is in the 142-SCC, not at L0.** The module imports *upward*: `adapters` and
> `codex_exec` (Execution, L1), `content_compression` (Call Corpus, L1), `pricing` (Reference Data, L0), and five
> `ledger_*` (`ledger_fork_locks`, `ledger_read`, `ledger_request_cursor`, `ledger_wal_maintenance`,
> `ledger_writer`). So L0 is the **target** once the §2 split lands; the shipped module sits in the L1 mesh. An
> earlier draft assumed `config` was a safe L0 leaf outside the cycle — it is not (caught by cross-check before any
> move; see §4b).

#### 3. Money Ledger · `STORE` · L0
Money truth: `spend_events` schema, single-owner hash-chained writer, migrations, indexes, fork-safe locks, rollups,
WAL upkeep, dead-letter replay, legacy-charge migration.
`ledger.py` · `ledger_writer.py` · `ledger_schema_migrations.py` · `schema_indexes.py` · `ledger_fork_locks.py` ·
`ledger_daily_rollup.py` · `ledger_wal_maintenance.py` · `ledger_deadletter.py` · `migrate_charges.py`

#### 4. Spend Search · `STORE` · L1
Bounded, admission-counted, read-only ledger queries with per-request deadlines shared by every skin.
`ledger_read.py` · `ledger_request_cursor.py` · `ledger_deadline_polling.py`

#### 5. Call Corpus & Content Retention · `STORE` · L1
A **second** system of record, distinct from money: per-call context/quality corpus (prompts, outputs, evidence),
age-bounded complete-content tier, lossless compression, routing-audit metadata. Holds content, not dollars — which
is why it cannot live inside Money Ledger.
`calls.py` · `callio.py` · `content_retention.py` · `content_compression.py` · `content_compression_backfill.py` ·
`routing_audit.py`

### GENERATE — produces spend, data, or work

#### 6. Spend Capture & Ingestion · `GENERATE` · L1
Turns spend that happens **outside** spendguard's own execution into ledger/corpus rows: SDK/LiteLLM/HTTP/OTel
capture, transcript miners (Claude Code, Codex, claude.ai), the transcript-source and GPU ports with their provider
adapters, historical backfill, first-run scan.
`bedrock_adapter.py` · `litellm_adapter.py` · `vertex_adapter.py` · `http_capture.py` · `otel_ingest.py` ·
`claudecode.py` · `codex.py` · `chat.py` · `compaction.py` · `sources.py` · `scan.py` · `backfill.py` ·
`gpu_port.py` · `lambda_adapter.py` · `modal_adapter.py` · `runpod_adapter.py`

#### 7. Execution · `GENERATE` · L1
Spends tokens: provider registry and typed vendor calls, batch submit and batch-job lifecycle, and the
subscription-lane executors (one-shot CLI runs on flat-fee plans), plus the provider plugin contract.
`vendor_call.py` · `submit.py` · `batch_tracker.py` · `subscription_exec.py` · `codex_exec.py` · `kimi_exec.py` ·
`zai_exec.py` · `antigravity_exec.py` · `subscription_cli_results.py` · `provider_plugins.py` · `provider_kit.py`

#### 8. Work Integrity · `GENERATE` · L1
Correctness contracts on produced work, independent of how it is routed: every item ends in a named state, resume
never re-buys or reuses stale output, outputs match a declared shape, files enter prompts whole, commit assertions,
bulk-resilience rule.
`item_ledger.py` · `job_fingerprint.py` · `output_contract.py` · `llm_files.py` · `work_integrity.py` ·
`bulk_resilience.py`
*(`execution_journal.py` was reassigned to Token Operations #11 — it is mutually bound with `delegate_jobs`; see §4b.)*

### ACTIVELY-MANAGE — live decisions over money and resources, per call/job

#### 9. Admission & Enforcement · `ACTIVELY-MANAGE` · L1
The **allow/deny/defer** decision per call or job: pre-submit cap gate, test-first/estimate-first bulk gate,
escalation admission, exhausted-plan admission, model preflight, disk reserve, host-agent hooks (spawn, weekly cap,
residency), and the cache short-circuit.
`bulkgate.py` · `escalation_ladder.py` · `plan_admission.py` · `model_preflight.py` · `disk_guard.py` ·
`agent_spawn_gate.py` · `weekly_cap_guard.py` · `residency_gate.py` · `semcache.py`

#### 10. Routing · `ACTIVELY-MANAGE` · L1
**Which wallet pays:** target utility scores, marginal-cost economic routing, horizon allocation across
lane/batch/realtime, cascade escalation, best-value (model, effort) resolution, lane load sensing, bulk-tier config.
`route_utility.py` · `route_economics.py` · `route_horizon.py` · `cascade.py` · `best_value.py` · `lane_balance.py` ·
`tier_config.py`

> Routing and Admission are **two decisions**, not one: *which wallet* vs *allow/deny/defer*. The seed merged them;
> the evidence splits them.

#### 11. Token Operations · `ACTIVELY-MANAGE` · L1
Cross-call live resource management: dispatch governor, cross-process rate reservation, durable lane queue and
predictive planner, storm coalescing, delegation (router, durable jobs, exactly-once terminal execution journal,
detached worker, worktree isolation, reaper), warm-daemon pools and broker, persisted per-resource state, lane
fail-over evidence, corpus fan-out.
`dispatch.py` · `xp_rate_window.py` · `lane_queue.py` · `queue_planner.py` · `storm_coalescer.py` · `storm_submit.py` ·
`storm_route.py` · `delegate_router.py` · `delegate_jobs.py` · `execution_journal.py` · `delegate_worker.py` · `delegate_worktrees.py` ·
`process_group_reaper.py` · `warm_stdio_daemon.py` · `codex_daemon.py` · `codex_daemon_broker.py` ·
`codex_broker_owner.py` · `kimi_daemon.py` · `resource_state.py` · `lane_recovery.py` · `comprehend.py`

### ANALYZE — reads records, produces judgement/valuation/evidence (no live spend decision)

#### 12. Cost Estimation · `ANALYZE` · L1
**Pre-spend** projection: input/output token counting per provider and modality, naive estimate, learned
calibration, estimate-vs-bill divergence. Sits **below** admission (the gate consumes it) — it is not post-hoc
measurement, which is why it is its own context and not part of Measurement.
`estimate.py` · `calibrate.py` · `expected_output.py` · `content_tokens.py` · `provider_tokens.py` ·
`estimate_divergence.py`

#### 13. Treasury / Valuation · `ANALYZE` · L1–L2 (read-side)
Values money at spend time, across the three account classes: metered prepay balances (Stored-value: sunk vs
auto-reload), plan quota and headroom (Allowance), plan economics and $/token, plan value, and paid overage
(Arrears). `overage.py` lives here — it reports **live Arrears status**, not forensic accounting.
`balances.py` · `lane_quota.py` · `subscription_headroom_surfaces.py` · `lane_economics.py` · `lane_value.py` ·
`overage.py`

#### 14. Measurement & Experimentation · `ANALYZE` · L2
Produces empirical **evidence**: judged readings with provenance, bakeoffs, compare, A/B lab, equivalence ladder,
requirement judge, stability, effort titration, reachability, servability, realized routing/efficiency,
guarded-savings distribution, cache audit/test, prompt lint, practice review, lane eligibility.
`measurement.py` · `bakeoff.py` · `compare.py` · `experiment.py` · `equivalence.py` · `requirement_judge.py` ·
`stability.py` · `effort_titration.py` · `reliability.py` · `lane_servability.py` · `route_realized.py` ·
`realized.py` · `guard.py` · `cacheaudit.py` · `cachetest.py` · `prompts.py` · `review.py` · `lane_eligibility.py`

#### 15. Learning & Advisory · `ANALYZE` · L2
Turns evidence into **learnings and advice**: insights + temporal learning graph, re-validation, decaying contextual
bandit, cold-start prior, deterministic and LLM advisors, post-event conversation/history mining, cold-start
bootstrap, plan brief.
`learn.py` · `validate.py` · `lane_bandit.py` · `intent_model_prior.py` · `advise.py` · `advisor.py` · `conv.py` ·
`history.py` · `bootstrap.py` · `brief.py`

> Measurement and Learning are split (the seed held them as one "Measurement & Learning"). The economic loop names
> them as **separate stages**; one domain would hide the measurement→learning edge. `lane_bandit.py` is the learning
> **write-path** and lives here; its read side feeds Routing.

#### 16. Reconciliation & Attribution · `ANALYZE` · L3
Forensic accounting against **provider truth** (one reconcile loop + per-provider reconcilers, gap ownership, offline
owner, admin-usage oracle, trust check, cost-axis integrity, anomaly z-scores) and the third axis:
org→team×project attribution, tagging, work-done context.
`reconcile.py` · `ledger_sync.py` · `reconcile_openai.py` · `reconcile_anthropic.py` · `reconcile_calls.py` ·
`reconcile_estimate_stage.py` · `reconcile_gap_ownership.py` · `reconcile_maintenance_owner.py` ·
`realtime_oracle.py` · `trust.py` · `cost_integrity.py` · `anomaly.py` · `attribution.py` · `tag.py` · `workdone.py`

#### 17. Spend Reporting · `ANALYZE` · L3
**Computes** the unified daily/weekly/monthly report and the monthly-close statement. It owns totals, so it is **not**
a facade (the seed implied it inside Surfaces).
`report.py` · `close.py`

#### 18. Assurance & Self-Verification · `ANALYZE` · L3
Checks spendguard's **own** code and installation, not money: price-literal and token-cap scans with a model-verdict
store, key-shadow and venv coverage audits, consumer audit, metadata drift, forensic verify, architectural fitness,
readiness gates, ledger benchmarks, source compaction for review.
`audit.py` · `token_caps.py` · `estimate_literals.py` · `verdict_ledger.py` · `keys_audit.py` · `coverage.py` ·
`consumer_audit.py` · `metadata_audit.py` · `verify.py` · `architectural_fitness.py` · `capability_readiness.py` ·
`capability_readiness_gates.py` · `external_standard_readiness.py` · `ledger_performance_benchmark.py` ·
`source_compact.py`

### EGRESS — leaves the machine and owns what it scrubs/projects

#### 19. Federation & Egress · `EGRESS` · L3
Everything that leaves the host: client-side de-identification, SaaS seam, scrubbed efficiency signal and shared
insights, event sinks, email, FOCUS export. Owns scrubbing/projection logic, so it cannot be a thin INTERFACE.
`saas.py` · `deid.py` · `signal.py` · `share.py` · `emit.py` · `notify.py` · `focus_export.py`

### PLATFORM — the product operating itself

#### 20. Platform: Install, Fleet & Release · `PLATFORM` · L4
Self-operation of the package: version SSOT, release green pointer, distribution attestation, manifest, fleet
convergence, OS scheduler, init/config interview, lane activation, interpreter gating via run/remote, offline build
provisioning, launchd labels.
`_version.py` · `release.py` · `distribution.py` · `distribution_daemon.py` · `manifest.py` · `fleet.py` ·
`schedule.py` · `setup.py` · `lanes.py` · `runner.py` · `remote.py` · `offline_python_build.py` · `launchd_labels.py`

### INTERFACE — thin facades that own no capability

#### 21. Surfaces · `INTERFACE` · L4
Thin facades over the capability SSOT: cli, `__main__`, MCP server and its descriptor lifecycle, HTTP serve, the
stable `ask()` facade, capability registry and generated CLI/API/MCP/doc projections, shared UI strings.
`cli.py` · `__main__.py` · `mcp_server.py` · `mcp_fd_lifecycle.py` · `serve.py` · `crossllm.py` ·
`capability_registry.py` · `capability_cli_projection_models.py` · `capability_cli_parsing.py` ·
`capability_cli_callable_generation.py` · `capability_cli_dispatch_generation.py` ·
`capability_cli_help_generation.py` · `capability_surface_generation.py` · `capability_documentation.py` ·
`cli_arguments.py` · `ui.py`

---

## 2. Split modules (12)

These have more than one responsibility and are split across their true contexts (down from 106 flagged in v1 — the
rest were false positives). Each is a real refactor candidate; the context assignment above is the **target** home of
the dominant responsibility.

**Home layer (dominant responsibility) — these modules are placed here, not left un-layered.** §1 lists each module
once; the split modules below take the layer of their *dominant* context so the layering is complete:
`__init__`→L4 (Surfaces) · `adapters`→L1 (Execution) · `gate`→L1 (Admission) · `budget`→L1 (Admission) ·
`config`→L0 (Configuration — *target* layer; the shipped module is in the L1 SCC, see §1 #2 note + §4b) ·
`models`→L0 (Reference Data) · `whole_job`→L1 (Routing) · `lane_quota_cost`→L1
(Treasury) · `truth`→L3 (Reconciliation) · `receipt`→L3 (Spend Reporting) · `openai_batch_cli`→L1 (Execution) ·
`resources`→L1 (Spend Capture). The measured import cycle (§3) runs through exactly these hubs — `gate`, `adapters`
and `budget` are its three highest-degree nodes — which is why resolving their splits is the real decoupling lever,
not the `__init__` root.

| Module | Responsibility → context |
|---|---|
| `__init__.py` | public API re-exports → **Surfaces**; `_auto_install` gate side-effect → **Admission**; `install_litellm/bedrock/vertex` hooks → **Spend Capture**. *(The single highest-leverage split — see §3.)* |
| `adapters.py` | provider registry/call/vision/embed/deadlines/budget-healing → **Execution**; lane/lane-model cooling + learned rate limits → **Token Operations** (`resource_state`); best-value default + route-through-queue + fallback learning → **Routing**; `schema_capability`/`generation_support` per-model facts → **Reference Data**. |
| `gate.py` | pre-submit estimate-vs-cap gate + kill switch → **Admission**; `_record_rt` realtime recording → **Spend Capture**. |
| `budget.py` | legacy cross-process charges store → **Money Ledger**; fleet-wide daily/monthly cap check → **Admission**. |
| `config.py` | paths/knobs/key resolution → **Configuration**; single-instance locks + ledger connection/WAL/cursor wiring → **Money Ledger**; plus measured upward imports into `adapters`/`codex_exec` (**Execution**) and `content_compression` (**Call Corpus**) — each should move behind the target context's surface. This upward wiring (wider than the ledger wiring alone) is why `config.py` is in the L1 SCC, not an L0 leaf — the split defect. |
| `models.py` | verified per-model facts/quirks → **Reference Data**; `apply_call_params` request shaping → **Execution**. |
| `whole_job.py` | choose batch/lane/metered per job, capability-matched → **Routing**; enforce budget + execute + return receipts → **Token Operations**. |
| `lane_quota_cost.py` | measured lane token cost → **Treasury**; fail-closed plan-quota admission → **Admission**. |
| `truth.py` | compute per-day provider-truth totals → **Reconciliation**; push totals to org server → **Federation & Egress**. |
| `receipt.py` | inline per-flow receipts + running tally → **Spend Reporting**; `_install_claude_code` hook wiring → **Platform**. |
| `openai_batch_cli.py` | build/submit/fetch batch envelopes → **Execution**; batch-submit/fetch command entrypoints → **Surfaces**. |
| `resources.py` | vast.ai GPU spend snapshot + day-splitting → **Spend Capture**; map GPU spend to org/team/project → **Reconciliation**. |

---

## 3. The economic loop — target vs the measured reality

**Target (the to-be this taxonomy organizes toward):** an acyclic flow
`ledger → measurement → learning → [valuation table] → treasury → routing → execution → ledger`,
with lower layers (L0 STORE) importing nothing above them.

**The code today is far from that — and the gap was mis-sized by the discovery.** A deterministic AST import-graph
analysis (Tarjan SCC over every intra-package import; not the LLM discovery's estimate) measured the real cycle:

> **One strongly-connected component of 142 modules** (65% of the 220) carrying **941 intra-SCC import edges.** The
> only other cycle is the 2-edge `kimi_daemon`↔`kimi_exec` pair.

The discovery's earlier "30-module / ~19-edge" claim was wrong by ~50× in edges — all 19 edges it named are real, but
they are **~2% of the cycle**. Cutting them would leave the hairball essentially intact. Three interventions were
measured against the real graph, and **none is a cheap lever:**

| Intervention | Effect on the 142-SCC |
|---|---|
| Remove all `__init__`/root-hub edges (the "highest-leverage" guess) | 142 → **134** (root has only 7 in / 14 out edges — a peripheral node, not the hub) |
| Remove **all 69** layer-inversion edges (lower-imports-higher) | 142 → ~132; 69 of 941 edges is 7% of the cycle |
| Remove **both** of the above together | still a **130-node SCC** — not a DAG |

**What the cycle actually is:** a dense **within-layer mesh**, not a ring and not a layering inversion. Of the 941
edges, **248 are same-layer** (dominantly within **L1**), routed through the central multi-responsibility hubs
`gate`, `adapters`, `budget` (the three highest-degree nodes), then `models`, `truth`, `resources`, `receipt`,
`whole_job`, `lane_quota_cost`. Breaking it is **not** an edge-cut sequence — it is sustained **within-layer
decoupling**, which must start by resolving those hubs' splits (§2) so the shared responsibilities stop binding every
L1 context to every other.

### Decision: contain now, do not cure blind
This is shipped (0.16.28) code with green CI; a 142-module decoupling done blind is a multi-week, high-regression
program for a maintainability (not functional) payoff. So:

1. **Guard first (Phase 0, bounded):** replace the existing `architectural_fitness` ratchet — which records 943
   cyclic edges and only fails on *new* ones, i.e. it **sanctions** today's hairball — with a **magnitude ratchet**:
   record the measured `{largest_scc_size, cyclic_edge_count}` as a baseline and fail the gate if either **grows**,
   auto-updating the baseline **down** when they shrink. Assert it against the **real** `spendguard` package, not the
   2-module scratch fixture the current test uses. This stops the cycle worsening and makes it honest.
2. **Decoupling is recorded as known debt, degree-ranked, not scheduled:** attack order when/if funded is the
   highest-degree hubs first — `gate`, then `adapters`, then `budget` — each by resolving its §2 split (move the
   secondary responsibility to its true context), with the Phase-0 ratchet proving each step shrank the SCC.

### Smaller debts (real, but minor next to the mesh)
- **`bulkgate` → `advisor`:** Admission consumes the learning advisor instead of a calibrated estimate (Cost
  Estimation). A genuine layer violation, cheap to fix independently.
- **Meta-loop:** 9 ANALYZE modules (`advisor`, `bakeoff`, `experiment`, `effort_titration`, `cacheaudit`,
  `cachetest`, `conv`, `review`, `intent_model_prior`) import `submit` (Execution) to buy their own judge calls — an
  import back-edge Execution ← Analyze; expose a caged **meta-call port** and depend on that.
- **Rule-4 at scale:** 80 modules import `capability_cli_parsing` and 44 define their own `main()` — the capability
  modules own their CLI, so the facades are not thin; move `main()` bodies into generated projections.

---

## 4. Corrections to the seed

The seed hypothesis named modules that **do not exist** in the 220-module corpus. For the record, and so they are not
cited as real again:

| Seed name | Reality |
|---|---|
| `executors` | split across the per-lane `*_exec.py` in Execution |
| `bulk_delegate` | delegation is `delegate_*` in Token Operations |
| `utilization` | no such module |
| `determinism` | lives in `stability.py` and `adapters` (`measurement=True`) |
| "money-ledger" / connection-pool | `ledger.py` + `ledger_writer.py` + `config.py`'s wiring (the split defect) |
| `savings` (analytics) | `guard.py` (guarded-savings distribution) |

---

## 4b. Ground-truth placement corrections (measured 2026-11-02)

Two §1/§2 placements were corrected against the **real** import graph (AST over `src/spendguard/*.py`), not inferred.
Both were caught by the migration-plan cross-check before any module was moved — the pre-registered pilot assumption
that `config` was a safe L0 leaf was false, which is why the plan now grounds every placement before acting.

- **`config.py` is in the 142-SCC, not an L0 leaf.** Measured imports (upward): `adapters` and `codex_exec`
  (Execution, L1), `content_compression` (Call Corpus, L1), `pricing` (Reference Data, L0), and five `ledger_*`
  (`ledger_fork_locks`, `ledger_read`, `ledger_request_cursor`, `ledger_wal_maintenance`, `ledger_writer`). L0 remains
  the **target** for the Configuration context once the §2 split lands; the shipped module is not there today.
- **`execution_journal.py` → Token Operations (#11), not Work Integrity (#8).** It is *mutually* imported with
  `delegate_jobs.py`: `delegate_jobs` calls `begin_durable_execution` / `recover_durable_execution` /
  `terminalize_durable_execution`, and `execution_journal` calls back into `record_delegate_codex_thread` /
  `register_codex_session_lease` / `settle_codex_session_lease` / `reap_terminal_codex_sessions`. The journal's
  dominant collaborator is delegation, so its true home is Token Operations. Both contexts are L1 — this does not
  change the layering, only the context assignment.

## 5. Open items (post-finalization)

- **#2 whole-job planner** — `submit(jobs, goal)` built on the capability core (Routing + Token Operations), per
  [`.claude/plans/zany-cooking-moon.md`](../.claude/plans/zany-cooking-moon.md).
- **Phase 0 acyclicity ratchet** (§3 decision) — the one scheduled follow-up: make `architectural_fitness` gate the
  cycle's *magnitude* against a shrink-only baseline, asserted on the real package. Bounded, low-risk.
- The 142-module SCC itself is **known debt**, not scheduled work — the §3 degree-ranked hub decoupling runs only if
  maintenance pain justifies the multi-week cost. The cycle exists in shipped 0.16.28 and is functional today.

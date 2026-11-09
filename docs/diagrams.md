# Architecture diagrams

## Active spend management and org truth flow

The active path acts before dispatch, records the economic outcome on three separate axes, and publishes scrubbed
operational truth to the website. Effective caps travel the other direction and are enforced locally
([`src/spendguard/manage/admission/plan_admission.py`](https://github.com/llmspendguard/llm-spendguard/blob/main/src/spendguard/manage/admission/plan_admission.py),
[`src/spendguard/manage/routing/route_utility.py`](https://github.com/llmspendguard/llm-spendguard/blob/main/src/spendguard/manage/routing/route_utility.py),
[`src/spendguard/manage/token_operations/delegate_router.py`](https://github.com/llmspendguard/llm-spendguard/blob/main/src/spendguard/manage/token_operations/delegate_router.py),
[`src/spendguard/analyze/measurement/guard.py`](https://github.com/llmspendguard/llm-spendguard/blob/main/src/spendguard/analyze/measurement/guard.py), [`src/spendguard/egress/saas.py`](https://github.com/llmspendguard/llm-spendguard/blob/main/src/spendguard/egress/saas.py)).

```mermaid
flowchart LR
    W[eligible work] --> P{plan admission}
    P -->|capped / overage / shed_lanes| S[shed from lane]
    P -->|ready| R[route]
    S --> R
    R --> BV[best-value: cheapest measured-good model + effort]
    R --> SS[served substitute: within anchor rate ×1.25]
    R -->|self-contained agentic subwork| D[delegate: codex / gemini / zai / kimi CLI]
    BV --> X[execute]
    SS --> X
    D --> X
    X --> SAVE[(ledger + decisions + savings)]
    SAVE --> AX["3 separate axes:<br/>real $ components :: est sub value :: savings"]
    SAVE --> SYNC[saas sync]
    F[fleet: green + stale consumers] --> SYNC
    SYNC -->|"status · truth · decisions<br/>shed · savings · fleet"| INGEST[server status / truth ingest]
    INGEST --> DASH[website dashboard]
    POLICY[server GET /v1/policy] -->|advisory / enforced effective caps| P
    CAT["published model_catalog.json<br/>row as_of"] --> CRON["server catalog cron<br/>global table + ingested_at"]
    CRON --> PUB["anonymous public API + /catalog<br/>no Clerk · no tenant RLS"]
    CRON -->|"authenticated overlay; preserve as_of"| SYNC
```

## End-to-end governed call and durable work flow

The synchronous and storm paths share admission and settlement concepts, but queue use is path-dependent: ordinary calls may record an open queue row when `advisor.route_through_queue` is enabled, while storm overflow reaches `lane_queue.db` through the durable batch executor. The explicit branches below avoid implying that every call is always queued ([`src/spendguard/generate/execution/adapters.py:688`](https://github.com/llmspendguard/llm-spendguard/blob/main/src/spendguard/generate/execution/adapters.py#L688), [`src/spendguard/generate/execution/schema.py:-71`](https://github.com/llmspendguard/llm-spendguard/blob/main/src/spendguard/generate/execution/schema.py#L-71), [`src/spendguard/generate/execution/schema.py:-5`](https://github.com/llmspendguard/llm-spendguard/blob/main/src/spendguard/generate/execution/schema.py#L-5), [`src/spendguard/manage/token_operations/storm_submit.py:155`](https://github.com/llmspendguard/llm-spendguard/blob/main/src/spendguard/manage/token_operations/storm_submit.py#L155)).

```mermaid
flowchart LR
    A[adapters.call] --> MS{model selection}
    MS -->|unnamed measurement| MR[ModelSelectionContractRefused]
    MS -->|labelled best-value| BV[cheapest measured-good model + effort]
    MS -->|other model=None| CD[configured default or cheapest priced chat]
    MS -->|named model| B{plan admission}
    BV --> B
    CD --> B
    B -->|healthy / proactive shed with no alternative| C[keep primary]
    B -->|ready alternative| D[redirect model and lane]
    B -->|capped or overage; no ready alternative| X[typed refusal]
    C --> E{metered storm eligible?}
    D --> E
    E -->|yes| F[storm coalescer]
    E -->|no| G[dispatch admission]
    F -->|sustainable share| G
    F -->|overflow / durable batch| H[(lane_queue.db)]
    G --> I[lane or metered API]
    H --> J[single-instance drain / batch collector]
    J --> I
    I --> K[settle result and ledger outcome]
    H -->|done / failed / dedup coalesce| K
```

## Three-database state model

`spend.db` is money-of-record, `lane_queue.db` is transient durable work state, and `fleet.db` is a pruned live-process registry. Queue connections receive WAL tuning and bounded archive maintenance; deploy and drain use separate process locks ([`src/spendguard/store/configuration/config_store.py:537`](https://github.com/llmspendguard/llm-spendguard/blob/main/src/spendguard/store/configuration/config_store.py#L537), [`src/spendguard/manage/token_operations/lane_queue.py:1127`](https://github.com/llmspendguard/llm-spendguard/blob/main/src/spendguard/manage/token_operations/lane_queue.py#L1127), [`src/spendguard/platform/fleet.py:419`](https://github.com/llmspendguard/llm-spendguard/blob/main/src/spendguard/platform/fleet.py#L419)).

```mermaid
flowchart TB
    SG[llm-spendguard runtime]
    SG --> S[(spend.db)]
    SG --> Q[(lane_queue.db)]
    SG --> F[(fleet.db)]

    S --> S1[spend ledger and attribution]
    S --> S2[money-of-record snapshots / reconciliation]

    Q --> Q1[pending, leased, queued_batch, done, failed]
    Q --> Q2[SHA-256 fingerprints and bounded results]
    Q --> Q3[WAL + checkpoint / size tuning]
    Q --> Q4[lane_queue_archive.jsonl + one rotated generation]
    Q --> Q5[lane_queue.drain.lock: one executor]

    F --> F1[one row per registered live PID]
    F --> F2[loaded SHA, version, command, launchd label]
    F --> F3[dead rows pruned on read]

    SG --> L[deploy.lock: one promotion gate]
```

## Release propagation

The comparison uses a process's frozen loaded SHA against the live green pointer. Managed consumers can be kickstarted; unsupervised consumers are warned, and supervised daemons can self-exit only when a fresh process is proven to land on green ([`src/spendguard/platform/fleet.py:553`](https://github.com/llmspendguard/llm-spendguard/blob/main/src/spendguard/platform/fleet.py#L553), [`src/spendguard/platform/fleet.py:647`](https://github.com/llmspendguard/llm-spendguard/blob/main/src/spendguard/platform/fleet.py#L647), [`src/spendguard/platform/release.py:136`](https://github.com/llmspendguard/llm-spendguard/blob/main/src/spendguard/platform/release.py#L136), [`src/spendguard/interface/cli.py:537`](https://github.com/llmspendguard/llm-spendguard/blob/main/src/spendguard/interface/cli.py#L537)).

```mermaid
sequenceDiagram
    participant O as Operator / deploy
    participant G as Green pointer
    participant F as fleet.db comparison
    participant M as Managed consumer
    participant U as Unsupervised consumer
    participant D as Self-checking daemon

    O->>O: acquire deploy lock and run gate
    O->>G: promote committed SHA
    O->>F: restart_stale(apply=true)
    F->>F: compare frozen served_sha to green SHA
    alt stale and launchd-managed
        F->>M: launchctl kickstart -k
        M-->>G: respawn and import green code
    else stale and unsupervised
        F-->>O: warn; manual restart required
        Note over U: Never killed automatically
    end
    D->>G: should_respawn at a safe point
    alt loaded SHA stale and live HEAD equals green
        D->>D: exit_if_stale()
        D-->>G: supervisor respawns on green
    else unknown or respawn would not land on green
        D->>D: keep running; avoid exit loop
    end
```

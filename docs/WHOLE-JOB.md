# The whole-job contract

**Hand spendguard the whole set of calls and your goal. It figures out the cheapest way to run them, runs them, and
returns.** You stop hand-tuning *which* model, *which* path, batch-or-not, lane-or-metered, per call — that
complexity moves into spendguard, where it belongs.

This is the recommended way to run any batch of LLM work where you care about cost. A per-call API can only optimise
one call at a time; when spendguard sees the *whole* set it can do things a per-call path structurally cannot — take
the Batch-API discount, spread the work across the subscription lanes you already pay for, and pick the globally
cheapest plan under one budget.

## The contract

```bash
# PLAN only — estimate the whole set, spend nothing ($0). The default.
spendguard submit-jobs jobs.jsonl

# EXECUTE — run it, capped by --budget (estimate-first: refuses before spending a cent over).
spendguard submit-jobs jobs.jsonl --execute --budget 5.00 --urgency auto
```

`jobs.jsonl` is one job per line:

```json
{"prompt": "Summarise this ticket …", "intent": "support-triage", "id": "t-1"}
{"prompt": "Classify sentiment …",    "intent": "support-triage", "id": "t-2", "schema": {"type": "object", "required": ["label"], "properties": {"label": {"type": "string"}}}}
```

- **`intent`** (required) is the job-type label — spendguard's routing *and* attribution key. Jobs that share an
  intent are planned together.
- **`schema`** (optional) is a JSON Schema. A *strict* one (it declares `required`/`nonempty` fields) is
  **capability-matched**: spendguard routes it to a path that can actually guarantee the shape — a provider that
  enforces it — instead of a lane that can only ask and might hand back prose. You get the shape reliably without
  asking for it specially.

The **goal** is how you tell spendguard what matters:

| Field | Meaning |
|---|---|
| `--budget` (`budget_usd`) | An **estimate-first hard cap.** The whole set is priced *before* any spend and refused if the estimate exceeds it — or if any group's cost can't be known (fail-closed: an unknown cost under a budget is a refusal, never a silent run). |
| `--urgency` (`auto`\|`realtime`\|`batch`) | `auto` lets the true-cost planner decide batch-vs-now; `realtime` keeps it synchronous; `batch` prefers the ~half-price Batch API where eligible. |
| `--quality` (`quality_bar`) | Run each group at *best-value* — spendguard picks the cheapest model whose measured quality holds for that intent, instead of you pinning one. |

## What you get back

```jsonc
{
  "results":  { "t-1": { "text": "…", "cost": 0.0007, "lane": "codex" }, … },   // ran synchronously
  "pending":  [ { "batch_id": "batch_abc", "intent": "support-triage", … } ],   // async (Batch API) — settle later
  "plan":     [ { "intent": "support-triage", "n": 2, "method": "realtime", "est_usd": 0.0014, "why": "…" } ],
  "receipt":  { "est_usd": 0.0014, "ran": 2, "pending": 0, "refused_code": null, … }
}
```

Async batch groups return a **handle**; settle them when they finish:

```bash
spendguard submit-jobs --collect ~/.spendguard/whole_job/run-….jsonl.pending.jsonl
```

Every submitted batch handle is written to a durable file the instant it is submitted — a batch is *paid* work, so
its handle survives a crash and is never silently lost.

## Where the economy comes from

spendguard already knows, per intent, the true marginal cost of each path — a subscription lane priced at what it
actually costs (never a fake $0), the metered API, and the Batch API at roughly half. The planner picks per group,
under your goal:

- **Subscription lanes you already pay for** are used wherever they genuinely fit — so a large, non-urgent set can
  run at little or no marginal cost instead of on the metered meter.
- **The Batch API** takes the non-urgent groups when it's cheaper (~half realtime).
- **The metered API** carries what needs it — a strict schema a lane can't guarantee, or an urgent call — and only
  what needs it.

You see the plan and the estimate before anything runs, and the receipt after.

## From an agent (MCP)

The `spendguard_run_jobs` MCP tool is the same contract, made safe for an agent to call: with **no budget** it
returns the plan and estimate only ($0) — never an unbounded fan — and a budget is required to execute (and is the
cap). So an agent can ask "what would this cost and how would you run it?" for free, and only spends inside a bound
you set.

## Safety, in one place

- **Estimate-first, fail-closed** — priced before spend; an over-estimate *or* an unknown cost under a budget is
  refused, with a structured `refused_code`.
- **No silent loss** — a failed batch submission is recorded (and the group still runs), durable batch handles
  survive a crash, and a spend refusal propagates rather than being swallowed.
- **Attribution preserved** — every job carries its `intent`; nothing is bucketed or swapped behind your back.

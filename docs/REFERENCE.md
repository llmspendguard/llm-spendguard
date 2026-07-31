# Reference — configuration, pricing, and every subsystem

The complete knob-by-knob reference: environment variables, caps by resource class, how pricing is layered and
kept fresh, the config file, the learned estimator, the learning advisor, observability, extending to any SDK,
and what de-identification does before anything leaves the machine.

This lives here rather than in the README so the front page stays readable; nothing was dropped in the move.

## Knobs (env)
`GATE_CAP=<$>` (default 75) · `GATE_ALLOW=1` (permit one over-cap run) · `GATE_DISABLE=1` (off for one run)
· `GATE_RT_BUDGET=<$>` (per-process realtime ceiling, default 50) · `SPENDGUARD_HOME=<dir>` (data/flag/log location,
default `~/.spendguard`) · `SPENDGUARD_ENV=<path>` (.env for keys)
· `SPENDGUARD_RECEIPTS=off|footer|flow|verbose` (inline-receipt verbosity, default `flow`; also `receipts.level` in config.json)
· `SPENDGUARD_RECEIPTS_SINK=stderr|stdout|file:<path>` (where the auto-receipt goes, comma-sep; also `receipts.sinks`)
· `SPENDGUARD_CC_DIR` / `SPENDGUARD_CODEX_DIR` (override the Claude Code / Codex session dirs for est-value mining)
· `SPENDGUARD_NO_AUTOINSTALL=1` (don't gate on `import spendguard`) · `SPENDGUARD_REQUIRE=1` (fail-closed import —
raise if an SDK is present but the gate can't enforce) · `SPENDGUARD_ALLOW_ANON=1` (allow team push with a
non-email contributor; off by default so anon ids can't create phantom members)
· **batch-1 gate:** `GATE_BATCH1_MIN` (req count = "large", default 50) · `GATE_BATCH1_USD` (or ≥ this $, default 5)
· `GATE_BATCH1_DAYS` (look-back for a prior test, default 14) · `GATE_REQUIRE_BATCH1=1` (refuse, don't just warn) ·
`GATE_NO_BATCH1=1` (disable) — warns/refuses a large batch for an intent with no recent realtime/batch-1 test

## Caps by resource class (LLM · compute · total)
Beyond the per-batch cap, spendguard tracks **cumulative** spend caps split by *what's spending* — so you can
set a tight LLM sub-limit under a higher overall ceiling. Each class has a `daily` and a `monthly` window
(`null` = off), stored in `config.json` under `caps`, with an env override for every one:

| Cap | Config (nested or flat) | Env | Behaviour |
|---|---|---|---|
| **LLM** daily / monthly | `caps.llm.{daily,monthly}` | `GATE_LLM_DAILY` · `GATE_LLM_MONTHLY` | **HARD — gate-enforced** (OpenAI + Anthropic calls hit the gate) |
| **Compute** daily / monthly | `caps.compute.{daily,monthly}` | `GATE_COMPUTE_DAILY` · `GATE_COMPUTE_MONTHLY` | **alert-only** (remote-compute / vast.ai launches don't pass through the gate — surfaced in the report + dashboard) |
| **Total** daily / monthly | `caps.total.{daily,monthly}` | `GATE_TOTAL_DAILY` · `GATE_TOTAL_MONTHLY` | overall ceiling (LLM + compute) |

These need `budget.backend = sqlite` (the cross-process ledger). The **legacy flat `caps.daily` / `caps.monthly`**
still work and are honored as the **total** ceiling. (Config storage accepts either the nested `caps.llm.daily`
or the flat `caps["llm.daily"]` form — see `config.class_cap` / `config_schema.py`.)

## Pricing: layered, broad, low-maintenance
Prices load in layers, lowest→highest precedence — so you get **~2,300 priced models across 80+ providers** for
free, your hand-verified rates always win, and you can override anything:

1. **LiteLLM community dataset** (breadth + freshness) — `spendguard sync-prices` fetches
   [LiteLLM's CI-maintained `model_prices_and_context_window.json`](https://github.com/BerriAI/litellm/blob/main/model_prices_and_context_window.json)
   (a ~2,700-entry dataset; the ~2,300 entries that carry a token price become priced models, 80+ providers),
   validates it (refuses an empty/bad fetch), and caches it to `~/.spendguard/litellm_prices.json`. Read from
   cache only — **no network at import**.
2. **Curated `prices.json`** (shipped in the package) — your verified models (gpt-5.5, opus-4.8, …) override LiteLLM.
3. **User override** — `~/.spendguard/prices.json` / `.yaml` / `$SPENDGUARD_PRICES` wins over everything.

If nothing loads, a built-in table in `pricing.py` is the final fallback (never breaks). Run `spendguard sync-prices`
once (and periodically) to refresh; that's the primary freshness mechanism — `check-prices`/`refresh-prices` are backups.

## Configuration

**Where everything lives.** `pip install llm-spendguard` installs only the **code** (into your environment's
`site-packages`). All of your **config + data** lives under **`~/.spendguard/`** (override with `SPENDGUARD_HOME`) —
created by `spendguard init` and at runtime, **never by pip**. You only ever edit **two** files; the rest is
auto-generated (logs, ledger, caches) and safe to ignore or delete.

| file | what it is | you edit? |
|---|---|---|
| `config.json` | caps · `gate.enforce` · `deid` · budget · saas settings | ✎ **yes** (or `spendguard init`) |
| `keys.env` | secrets — LLM / vast.ai / org keys | ✎ **yes** (paste your keys) |
| `saas.json` | team/org connection, if you connect one | `init --connect` |
| `email.json` | daily-report email (optional) | `init` |
| `prices.json` | your price overrides (optional; wins over the shipped table) | optional |
| `spend.db` | the SQLite spend ledger (when `budget.backend=sqlite`) | auto |
| `gate_log.jsonl` · `realtime_log.jsonl` · `report.log` | audit logs | auto |
| `identity.json` · `*_state.json` · `*_cache.json` | contributor id + caches/state | auto |
| `disabled` | kill-switch flag (`spendguard off` touches it) | auto |

A real **environment variable always overrides** the files (so prod / CI / secret-managers Just Work), and
`spendguard config` prints the resolved value + source for every setting. The two files you edit:

**① `keys.env` — secrets** (chmod 600). Loaded into the environment on `import spendguard`, so your **own**
`openai.OpenAI()` / `anthropic.Anthropic()` calls pick the keys up too — no separate export needed. Fill only the
blanks you use:
```bash
OPENAI_API_KEY=            # + ANTHROPIC_API_KEY / GEMINI_API_KEY / DEEPSEEK_API_KEY / DASHSCOPE_API_KEY
VAST_API_KEY=              # remote GPU compute (vast.ai), metered into the same ledger
RUNPOD_API_KEY=            # + MODAL_TOKEN_ID/MODAL_TOKEN_SECRET / LAMBDA_API_KEY — more GPU clouds, same ledger
SPENDGUARD_SAAS_KEY=       # team/org roll-up key from llmspendguard.com (optional)
```

**② `config.json` — operational (non-secret)**:
```jsonc
{
  "caps": { "per_batch": 75, "realtime": 50, "meta": 2.0,           // $ — meta = spendguard's own advisor budget
            "llm": {"monthly": null}, "compute": {"monthly": null}, "total": {"monthly": null} },
  "gate": { "enforce": "warn" },        // the estimate→test→run rail
  "deid": { "engine": "regex" },        // redact text that leaves this machine
  "saas": { "enabled": false, "visibility": "team", "sync_interval": "daily", "project": null }
}
```

**Enums (the exact strings):**
| setting | values | meaning |
|---|---|---|
| `gate.enforce` | `off` · `warn` · `block` | test-first rail — `off` = none; `warn` = log a "would-block" when a big batch runs without a fresh estimate+test *(default)*; `block` = hard-require estimate → test first |
| `deid.engine` | `regex` · `presidio` · `off` | egress de-id — regex floor *(default)* · floor + Presidio NER · none |
| `saas.visibility` | `private` · `team` · `org` | how far your scrubbed roll-up goes (`private` = nothing leaves) |
| `saas.sync_interval` | `off` · `hourly` · `daily` · `weekly` | roll-up push cadence |
| `budget.backend` | `memory` · `sqlite` | per-process cap vs a shared cross-process ledger |

**Budgets:** `caps.meta` = spendguard's own advisor spend; `caps.{llm,compute,total}.{daily,monthly}` = your workload
ceilings; **per-repo** = tag the repo via `saas.project` (or a repo-local `.spendguard.json`) and set org/team caps
centrally in the dashboard. The full registry — every setting, default, valid options, secret-or-not — is
`src/spendguard/config_schema.py` (it drives `spendguard init` and `spendguard config`).

## Pricing configuration
The curated/override files use this structure (`src/spendguard/prices.json`, `~/.spendguard/prices.json`, or `$SPENDGUARD_PRICES`):
```json
{ "_meta": {"verified": "2026-06-13", "source": "https://…", "stale_after_days": 45},
  "providers": {
    "openai":    {"models": {"gpt-5.5": {"in_": 5.0, "out": 30.0, "cached_in": 0.5, "batch_in": 2.5, "batch_out": 15.0}}},
    "anthropic": {"models": {"claude-opus-4-8": {"in_": 5.0, "out": 25.0, "cached_in": 0.5, "batch_in": 2.5, "batch_out": 12.5}}}
  }}
```
Add a provider/model by adding an entry. A user-override file only needs the models it changes. `spendguard providers`
lists what's configured. If the config can't load, the built-in table in `pricing.py` is the fallback (never breaks).

## Pricing freshness
Prices drift, and a wrong price is the bug that started this project. `spendguard check-prices` shows the
`verified` date and flags the table **STALE** once it's older than `stale_after_days` (default 45); the daily
`spendguard report` prints the same warning. To refresh: re-verify against the `source` URL and bump the
`verified` date in `prices.json`. (A live fetch-and-diff against provider pricing pages is a planned addition.)

## Real-time budget
Batch cost is known before submit; real-time isn't (output tokens). So the real-time layer **accounts actual
usage after each call** (and logs it, so real-time spend shows in `report`) and **hard-stops before the next call**
once per-process cumulative spend crosses `GATE_RT_BUDGET` (default $50) — the runaway-loop guard.

## Email the report
`spendguard report --email` (or `--email-to addr`) emails the report so a scheduled run isn't missed.
Config lives in `~/.spendguard/email.json` (gitignored — safe for the secret) or env.

**Email needs a *gated* sender — this is universal, not a spendguard limitation.** Mail servers reject
unauthenticated senders, so every provider makes you prove ownership *somehow* before sending. Pick whichever
is least friction for you:

| Backend | What it takes (one-time) | DNS? | config |
|---|---|---|---|
| **Gmail / Workspace SMTP** | a 16-char app password (Google authenticates the send) | no | `{"host":"smtp.gmail.com","port":587,"user":"you@co.com","password":"<app pw>","to":"you@co.com"}` |
| **SendGrid (Twilio)** | "Single Sender Verification" — click a link in a confirm email | no | SMTP host `smtp.sendgrid.net`, or add a SendGrid backend |
| **Resend** | verify a domain (SPF/DKIM DNS records) for arbitrary recipients; or send only to your Resend signup email via `onboarding@resend.dev` | yes (for arbitrary recipients) | `{"provider":"resend","to":"you@co.com","from_":"reports@your-verified-domain","api_key":"re_…"}` |

**If it isn't configured, it gracefully no-ops** — `report` still prints (and the scheduled task still delivers in-app);
you'll just see `email not configured — skipping`. A *configured* backend that errors prints `EMAIL FAILED: <reason>`
(e.g. Resend's "verify a domain" message) without affecting the report. So leaving email unset is a fine default.

> **⚠️ Deliverability (shared senders land in spam).** Sending from a provider's *shared* address
> (e.g. Resend's `onboarding@resend.dev`) **sends fine but frequently lands in Gmail/Workspace Spam** — the
> domain has no alignment with yours, so receivers distrust it. The report *is* delivered; it's just filtered.
> Fixes, simplest first: **(1)** in Gmail, "Report as not spam" + a filter on the sender/subject set to
> *Never send to Spam*; **(2)** use **Gmail/Workspace SMTP** so it sends *as you* from inside Google (inbox, no DNS);
> **(3)** verify your own domain on the provider and send from it. Also note `api.resend.com` is behind Cloudflare,
> which 403s the default `urllib` User-Agent — spendguard sets one (don't strip it).

## Compare models (cost-per-result)
Run one prompt across providers and table **cost + latency + output** — spendguard's angle is
*cost-per-result* (for deep evals, use promptfoo). Real calls, metered by the gate:
```
spendguard compare --prompt "Summarize X in 3 bullets" \
  --models gpt-5.5,claude-opus-4-8,gemini-2.5-flash,deepseek-chat,qwen-max --show
```
Built-in providers: **openai, anthropic, gemini, deepseek, qwen, z.ai (GLM)** (all but Anthropic via their
OpenAI-compatible endpoints, so the gate already meters them). **Keys live in ONE place: `~/.spendguard/keys.env`**
(created by `spendguard init`, chmod 600, loaded into the environment on `import spendguard`) — a real environment
variable always wins, so CI/secret-managers are never clobbered. Per provider: `OPENAI_API_KEY`,
`ANTHROPIC_API_KEY`, `GEMINI_API_KEY`, `DEEPSEEK_API_KEY`, `DASHSCOPE_API_KEY` (Qwen), `ZAI_API_KEY` (z.ai/GLM),
`MOONSHOT_API_KEY` (Kimi). **Add another in one line:**
```python
from spendguard.adapters import register_provider
register_provider("together", "https://api.together.xyz/v1", "TOGETHER_API_KEY", ("meta-llama", "mistralai"))
```

**First-class provider adapters (capture, not just `compare`).** Beyond the `compare` harness above, spendguard
ships dedicated **spend-capture** adapters that record real usage into the same ledger the SDK gate uses — priced
through `pricing.py`, fail-open, no double-counting:
- **Azure OpenAI** — already covered by the OpenAI-SDK gate (Azure uses the OpenAI SDK); LiteLLM-routed Azure
  traffic is deliberately skipped by the adapter below so it's counted once.
- **LiteLLM** — `spendguard.install_litellm()` (after `import litellm`) registers a success-callback that captures
  spend for **anything LiteLLM normalizes** (Bedrock, Vertex/Gemini, Cohere, Mistral, …) via LiteLLM's own
  computed cost.
- **AWS Bedrock (direct boto3)** — `spendguard.install_bedrock()` (after `import boto3`) patches botocore's
  dispatch to record `Converse`/`InvokeModel` token usage. Not needed if you go through LiteLLM.
- **Google Vertex / Gemini (direct google-genai)** — `spendguard.install_vertex()` (after importing the SDK)
  captures `generate_content` usage (sync + async). Not needed if you go through LiteLLM.

Each adapter auto-wires at startup **only if its SDK is already imported**; the explicit `install_*()` call is the
reliable path (it force-imports and wires) since these are heavy/optional deps the startup gate won't force-load.

## Call context & cost-per-good-result (opt-in)
Beyond *cost*, spendguard can record per-call **context** to build a cost+**quality** corpus. Off by default
(it can store prompts/outputs — privacy). Enable `calls.enabled` (+ `calls.store_prompts` for snippets and the
implicit signal).
- **Tag intent:** `with spendguard.context(intent="loinc-typing", chain="run-42"): ...`
- **Quality is deferred** — you can't judge an output when it's made, but the *next* call reveals it:
  - *automatic ("used"):* a later call in the same chain that reuses an output marks it good.
  - *explicit / judge:* `spendguard.feedback(call_id, ok=True, source="judge")` — capture the verdicts you already produce.
- **`spendguard calls`** → per intent: calls, $, good%, and **$/good (cost-per-good-result)** — the efficiency metric.

Real-time calls are recorded automatically (caller, prompt/output snippets, latency); batches record job-level.

### Smart attribution (a clean P&L, no manual bookkeeping)
Every charge is tagged on **two orthogonal dimensions**, so you can slice spend by either without bookkeeping:
- **WHO** — `org → team → contributor`, which **rolls up** the hierarchy. The contributor is set per install
  (default: git `user.email`); the org/team is resolved server-side from the connection key.
- **WHAT** — `project · intent · resource` (the repo/work, the labeled task, and whether it's LLM or
  remote-compute GPU).

Tagging is automatic: a project is inferred from the repo/cwd, refined by the call corpus's intent/caller and
the conversation that ran each batch; remote-compute rows route by instance label. The still-ambiguous
remainder can be resolved by a small, **capped, estimate-first** LLM pass (never auto-run). The result is a
clean P&L by team / project / intent with no manual entry. (Mechanism: `tag.py` cascade, `signal.py` per
project·intent·model roll-up, `conv.py` batch→conversation attribution, `saas.py` `org→team→user` push.)

## Learned cost estimator — *spendguard learns to estimate YOUR work correctly* (`spendguard calibrate`)
The naive estimate (input ≈ len(prompt)/4 · output = max_tokens · flat price) misses in a predictable,
per-activity way: models rarely fill max_tokens, tokenizers differ, batch ≠ realtime, caching lands.
`calibrate` closes the loop with the actuals spendguard already captures — per (activity, model) quantile
calibrations (output-fill, out-per-in, $-residual, input-ratio) with empirical-Bayes shrinkage across an
exact → model → global hierarchy. Zero LLM spend per estimate; prices only from `pricing.py`; every answer
carries its confidence (`level`, `n_obs`). Verified by a held-out backtest (`calibrate backtest`) that must
beat naive MAPE — on the reference corpus: overall 18%→10%, worst cells 2978%→23%.

**The org learns together** (`calibrate push` / `calibrate fetch`, auto on the daily report when a team
server is connected): each member shares only SUFFICIENT STATISTICS — per (label, model, transport,
quantity) `{n, p50, p90}`, labels de-identified, never prompts/outputs/$ — and pulls back the n-weighted
org aggregate. The org's experience becomes each member's estimation PRIOR; your own history always sits
on top (an exact-label org cell outranks your generic cross-model pools, never your own cell evidence).
Visibility-gated: `visibility=private` shares nothing.

Any job runner wires in with three calls — **no spendguard-side changes per consumer**:
```python
from spendguard import calibrate, calls

est = calibrate.estimate("stage:describe", n=1200, model="gpt-5.5", transport="batch",
                         est_in_tokens=prompt_tokens, est_out_max=max_tokens)   # plan with p50/p90
calibrate.record_estimate(job_id, "stage:describe", "gpt-5.5", est["p50_usd"],
                          est_in=prompt_tokens, est_out_max=max_tokens, n=1200, transport="batch")
with calls.context(intent="stage:describe", chain=job_id):                      # exact pairing key
    run_the_job()          # the gate captures the actuals; `spendguard report` pairs + sharpens nightly
```

## Learning advisor — *recommend considering history* (Layer 1 deterministic · Layer 2 LLM)
- **`spendguard advise [--intent X] [--plan MODEL]`** — pure-SQL ranking of your corpus by `$/good` (or `$/M out`
  when quality isn't labeled yet), confidence-weighted, with caveats. No LLM, no spend.
- **`spendguard backtest --as-of DATE`** — replays `advise` as of a past date (would it have caught known-good calls?).
- **`spendguard backfill`** — seeds the corpus + learning graph from your real batch ledgers (no spend).
- **Layer 2 (its own, *caged*, LLM use)** — every op is **estimate-only by default**; `--run` spends, and each paid
  call is tagged `intent=spendguard:*` so it hits a **separate meta budget** (`caps.meta`, default **$2/day**), is kept
  out of your workload budget, and is excluded from the corpus it analyzes:
  - **`spendguard mine`** — synthesize confidence-scored **insights** + learning-graph nodes from the evidence (reasoner).
  - **`spendguard optimize [--intent X] [--plan MODEL]`** — an actionable recommendation citing evidence + insights (reasoner).
  - **`spendguard reconstruct`** — judge a bounded sample of recovered call I/O for quality → real `good%`/`$/good`.
  - **`spendguard review`** — **practice audit**: judges whether usage was *smart*, not just what it cost. Assembles a
    context bundle (cost + quality + token-ratio + sample I/O + linked chat notes) and emits **conditional** insights
    (IF task_class/regime THEN action BECAUSE mechanism) — needs no ground truth, so it's robust where output-judging isn't.
  - **Models are configurable:** `advisor.model` (reasoner, default Opus 4.8) · `advisor.judge_model` (judge, default
    Haiku 4.5) — any priced model / provider. Run any op without `--run` to see the projected cost first.

### Cold start, quality corpus, living insights, collective learning
- **`spendguard bootstrap`** — the cold-start process: mine **all** history (ledgers → intents → graph → provider I/O →
  conversation) for free, then estimate the caged reasoning. One command, history → corpus → insights.
- **`spendguard fetch-io`** — recover the **real prompts+outputs** from the providers (OpenAI batch input/output files,
  streamed with early-stop; Anthropic results within 29 days) into a bounded `call_io` sample. **Zero token cost.**
- **`spendguard validate`** — **living insights**: re-checks each learning against the current corpus and moves it through
  its lifecycle (corroborated → `active` + confidence up; cited model gone / gap inverted → `refuted`/`superseded`). The
  advisor weights by *current* confidence + status, so stale advice sinks as data grows.
- **`spendguard insights {list,export,import}`** — **collective learning, opt-in + scrubbed**. Export *abstracts* insights
  into generalizable rules (keeps task_class/regime, model names, ratios; strips `$` amounts, intent names, evidence) and
  **previews exactly what would leave**. Import brings community rules in as **low-trust priors** that must be locally
  corroborated by `validate` before they sway the advisor.

> **On quality:** a cheap call that fails quality is wasted money, so cost-per-**good**-result is the metric. Two signals are
> trustworthy: **approach-quality** (`review` — needs no ground truth) and **outcome** (the conversation showing an output was
> used or redone). Judging output *correctness* in isolation is **not** reliable (an LLM can't verify a value it has no ground
> truth for) — spendguard quarantines such labels rather than trusting them.
- **Post-event mining (deterministic, zero spend)** — recover what the live recorder missed:
  - **`spendguard mine-history {intents,graph,git}`** — reconstruct each batch's **intent** from repo artifacts
    (`*batch_id*.json` + a size-bounded content scan of `data/`), add causal graph edges (`preceded`,
    `derived_from`), and read git history for cost/fix signals. `--apply` writes; report-only otherwise.
  - **`spendguard mine-conv {index,synth}`** — mine session transcripts for cost decisions. `index` is cached
    (deterministic); `synth` is the caged reasoner turning the top decision snippets into `source='conversation'`
    insights (estimate-first). Reconstructs your actual playbook (packing, never-cancel, price-basis errors, …).

## Observability (feed your existing stack)
spendguard emits an event per gated call — it's the *enforcement* layer, not another dashboard; route the
events to whatever you already run. Three sinks, all optional, none ever block or break the gate:
- **In-process callback:** `spendguard.on_event(lambda e: log(e))`
- **Webhook:** `emit.webhook` in `~/.spendguard/config.json` or `$SPENDGUARD_WEBHOOK` — POSTs the event JSON (Slack, your collector, …)
- **OpenTelemetry:** `emit.otel: true` / `$SPENDGUARD_OTEL` — a `spendguard.cost_usd` counter (needs `opentelemetry-sdk`)

Event shape: `{ts, kind: batch|realtime, provider, model, cost, decision}`. Webhook/OTel run on a background
daemon thread (drop-if-flooded), so even high-volume real-time calls aren't slowed; callbacks run inline (keep them fast).

## Extend to any SDK (zero required deps, fail-open)
spendguard ships with the OpenAI + Anthropic overlays, but the gate is generic — you can put **any** SDK under
it without adding a dependency:
1. **Intercept it:** `spendguard.register(module_path, ClassName, method, gate_fn)` patches that SDK's call
   method (e.g. `register("cohere", "Client", "chat", gate_fn)`). Write a small `gate_fn` that reads the request
   shape and estimates cost; add the model's prices to the table (`prices.json` / your override).
2. **Add an OpenAI-compatible provider in one line** (for `compare` + metering — most providers expose one):
   `from spendguard.adapters import register_provider; register_provider("together", "https://api.together.xyz/v1", "TOGETHER_API_KEY", ("meta-llama", "mistralai"))`.
3. **Emit anywhere:** route the per-call event to a webhook, OpenTelemetry, or an in-process callback
   (`spendguard.on_event(...)`) — see [Observability](#observability-feed-your-existing-stack).

All of it is **fail-open** (an estimation/patch error logs and lets the call proceed) and needs **no required
dependencies** — the SDKs and OTel are optional extras.

## Privacy — what leaves this machine (de-identification)
Nothing leaves until you opt in past `visibility=private`, and the roll-up itself carries only **scrubbed
aggregates** — never prompts, outputs, or `$` amounts. The little prose that *does* sync (generalizable insight
rules, git commit subjects, a caged "what was accomplished" summary) passes through a deterministic **de-id floor
at the wire**: emails, phones, SSNs, credit cards (Luhn-checked), IPs, API keys / bearer tokens / JWTs, and
private-key blocks become typed tags (`<EMAIL>`, `<API_KEY>`, …) — while the generalizable signal (ratios like
"26x", model names) is kept. Configurable + opt-in:
- `deid.engine=regex` — the built-in floor (default, **zero deps**).
- `deid.engine=presidio` — adds Microsoft Presidio NER for names / locations / dates (`pip install
  llm-spendguard[deid]`; if it isn't installed it degrades to the floor and warns once — egress is never blocked).
- `deid.engine=off` — no redaction (a deliberate footgun for fully-trusted private data).

De-id is local, fails **open toward privacy** (on any error the floor still runs), and is a tool *toward* HIPAA
Safe Harbor — not compliance by itself (you still need a BAA). It's a safety/extraction step, so it's regex + NER,
not an LLM — the agentic decisions (project / intent / quality) stay with the model.


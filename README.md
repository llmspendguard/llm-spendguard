# spendguard

Provider-agnostic LLM cost discipline for batch (and soon real-time) workloads.
Born from a real incident: a "cost-conscious" day that was supposed to cost ~$33 actually
cost **$149.76** — because a price constant was hardcoded wrong (GPT-5.5 at the old GPT-5
rate) and jobs ran 1 item per request (the shared prompt re-billed every call). spendguard
makes those mistakes impossible to ship silently.

## What it does
- **pricing** — one canonical, verifiable price table (OpenAI + Anthropic). Never hardcode a price again.
- **gate** — an overlay on the OpenAI/Anthropic SDKs (auto-installable via `sitecustomize.py`) that
  estimates every batch's cost, logs it, and **hard-stops** any single batch over a cap — then asks, if interactive.
- **estimate** — project a full job across models × packing *before* you spend.
- **reconcile** — actual $ from real billed tokens (OpenAI batch usage; Anthropic batch results), cache-aware.
- **report** — daily / weekly / monthly spend across providers, for a scheduled monitor.
- **audit** — fail CI if any code disagrees with the canonical price table.

## Use
```python
import spendguard
spendguard.install(cap=75)          # gate every batch submission in this process
```
Or auto-install for every process in a venv — drop this in `sitecustomize.py`:
```python
import spendguard; spendguard.install()
```

## CLI
```
spendguard status | on | off                 # kill switch (persistent flag)
spendguard report --alert-threshold 150
spendguard reconcile openai --by-day
spendguard estimate --items 263000 --from-sample test.jsonl --packs 1,30
spendguard audit --ci
spendguard pricing
```

## Knobs (env)
`GATE_CAP=<$>` (default 75) · `GATE_ALLOW=1` (permit one over-cap run) · `GATE_DISABLE=1` (off for one run)
· `SPENDGUARD_HOME=<dir>` (data/flag/log location, default `~/.spendguard`) · `SPENDGUARD_ENV=<path>` (.env for keys)

## Pricing: layered, broad, low-maintenance
Prices load in layers, lowest→highest precedence — so you get **2,700+ models across all providers** for free,
your hand-verified rates always win, and you can override anything:

1. **LiteLLM community dataset** (breadth + freshness) — `spendguard sync-prices` fetches
   [LiteLLM's CI-maintained `model_prices_and_context_window.json`](https://github.com/BerriAI/litellm/blob/main/model_prices_and_context_window.json)
   (~2,300 priced models, 80+ providers), validates it (refuses an empty/bad fetch), and caches it to
   `~/.spendguard/litellm_prices.json`. Read from cache only — **no network at import**.
2. **Curated `prices.json`** (shipped in the package) — your verified models (gpt-5.5, opus-4.8, …) override LiteLLM.
3. **User override** — `~/.spendguard/prices.json` / `.yaml` / `$SPENDGUARD_PRICES` wins over everything.

If nothing loads, a built-in table in `pricing.py` is the final fallback (never breaks). Run `spendguard sync-prices`
once (and periodically) to refresh; that's the primary freshness mechanism — `check-prices`/`refresh-prices` are backups.

## Configuration (prices, providers, models)
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
Built-in providers: **openai, anthropic, gemini, deepseek, qwen** (most via their OpenAI-compatible
endpoints, so the gate already meters them). Keys resolve per provider from env / `~/.spendguard` / `./.env`
(`OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, `GEMINI_API_KEY`, `DEEPSEEK_API_KEY`, `DASHSCOPE_API_KEY` for Qwen).
**Add another in one line:**
```python
from spendguard.adapters import register_provider
register_provider("together", "https://api.together.xyz/v1", "TOGETHER_API_KEY", ("meta-llama", "mistralai"))
```

## Observability (feed your existing stack)
spendguard emits an event per gated call — it's the *enforcement* layer, not another dashboard; route the
events to whatever you already run. Three sinks, all optional, none ever block or break the gate:
- **In-process callback:** `spendguard.on_event(lambda e: log(e))`
- **Webhook:** `emit.webhook` in `~/.spendguard/config.json` or `$SPENDGUARD_WEBHOOK` — POSTs the event JSON (Slack, your collector, …)
- **OpenTelemetry:** `emit.otel: true` / `$SPENDGUARD_OTEL` — a `spendguard.cost_usd` counter (needs `opentelemetry-sdk`)

Event shape: `{ts, kind: batch|realtime, provider, model, cost, decision}`. Webhook/OTel run on a background
daemon thread (drop-if-flooded), so even high-volume real-time calls aren't slowed; callbacks run inline (keep them fast).

## Extending to a new SDK
Add one interceptor: `spendguard.register(module, ClassName, "create", gate_fn)`, write a small
estimator for that SDK's request shape, and add its prices to `pricing.py`.

## Safety
Fail-**open**: any estimation or patch error logs a warning and lets the call proceed — the gate
never breaks a job by accident. Only the deliberate over-cap stop blocks. Disable instantly with
`spendguard off` (checked per-call, live) — and the kill switch is honored even if the gate itself errors.

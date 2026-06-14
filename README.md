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

## Configuration (prices, providers, models)
Prices live in a config file, not in code — `src/spendguard/prices.json` (shipped default), overridable by
`~/.spendguard/prices.json`, `~/.spendguard/prices.yaml` (needs PyYAML), or `$SPENDGUARD_PRICES`. Structure:
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

## Extending to a new SDK
Add one interceptor: `spendguard.register(module, ClassName, "create", gate_fn)`, write a small
estimator for that SDK's request shape, and add its prices to `pricing.py`.

## Safety
Fail-**open**: any estimation or patch error logs a warning and lets the call proceed — the gate
never breaks a job by accident. Only the deliberate over-cap stop blocks. Disable instantly with
`spendguard off` (checked per-call, live) — and the kill switch is honored even if the gate itself errors.

# llm-spendguard

A pre-spend **governor** for LLM API cost (OpenAI + Anthropic): it caps every call before the spend,
prices from a verified table, and **learns the cheapest config that still keeps quality** — then proves
and enforces it. Zero required dependencies; install is one line; it never breaks a job (fail-open).
Learn more at https://llmspendguard.com · **[Docs & quickstart →](https://docs.llmspendguard.com/)**

> 📘 **New here? Read the [Solution Specification](docs/SOLUTION-SPEC.md)** — the whole story end to end: why it
> exists, the value, the journey of a dollar (call → gate → ledger → reconcile → push), the design, and how it's
> tested, secured, and operated.

## Why llm-spendguard?
Cost overruns don't announce themselves — they slip in silently: a hardcoded price that drifted from the
real rate, a forgotten model swap, under-batching that re-bills a shared prompt every request, a job
cancelled "to save money" that still bills for completed work, an ungated script in some other venv quietly
leaking spend. spendguard stops those before the spend (the gate hard-stops over a cap, prices from a
verified table, finds the leaks) **and** learns what was actually worth it — so "cheaper" never quietly
costs you quality.

Born from a real incident: a "cost-conscious" day meant to cost ~$33 actually cost **$149.76** — a price
constant was hardcoded wrong (GPT-5.5 at the old GPT-5 rate) and jobs ran 1 item/request (the shared prompt
re-billed every call). spendguard makes those mistakes impossible to ship silently — and goes further:
it reconstructs *what* you should do cheaper, and won't let "cheaper" cost you quality.

## What it does
**Enforce → see → plan → prove → learn.**
- **gate** — overlay on the OpenAI/Anthropic SDKs (auto-installs via `sitecustomize.py`): estimates every
  batch/real-time call, **hard-stops** over a cap (per-batch + cross-process daily/monthly) — then *asks* if interactive.
- **pricing** — one canonical, verifiable table (layered from LiteLLM + curated + override), cross-checked
  vs OpenRouter; an `audit` fails CI if any code hardcodes a disagreeing price.
- **reconcile** — actual $ from real billed tokens; **`reconcile-ledger`** compares the local ledger to
  provider billing to find **leaks** (ungoverned spend from a non-gated venv/repo).
- **report** — daily/weekly/monthly email with spend totals + a leak alert + the advisor's top learnings.
- **learning advisor** — a per-call cost+quality corpus → confidence-scored, lifecycle-tracked **insights**;
  `brief` pre-fills a plan, `optimize` recommends the cheapest config that held quality, `experiment` proves
  it (cost↓ **and** same-output), `promote` runs it and keeps the output. Cost-per-**good**-result, not per-token.
- **cost levers** — prompt-caching audit/test, semantic cache + batch dedup, cost-aware cascade routing.
- **observability** — emits OpenTelemetry GenAI-convention metrics+spans → Langfuse / Helicone / Phoenix / any OTLP backend.

The advisor's own LLM use is itself **caged** (a separate `caps.meta` budget, tagged `spendguard:*`, excluded
from the corpus it analyzes) so the governor can't overspend governing.

**Docs:** [Architecture + diagrams](docs/ARCHITECTURE.md) · [Use with Claude/Cursor](docs/USING-WITH-CLAUDE.md) · [Methodology](docs/README.md) · [Roadmap (teams/orgs/SaaS)](docs/ROADMAP.md) · [Module map](src/spendguard/README.md) · [Contributing](CONTRIBUTING.md) · [Changelog](CHANGELOG.md) · [Setup](SETUP.md)

**Use with an AI assistant:** `spendguard install-rule --global` writes a rule into `CLAUDE.md` so **every** Claude/Cursor conversation routes the LLM code it builds through spendguard — then `spendguard install-skills` adds `/spend` (status) and `/spendguard-learn` (advisor) as slash-commands. See [Use with Claude](docs/USING-WITH-CLAUDE.md).
**Teams & orgs:** each user keeps their own ledger + sets their own caps (partner, not supervisor); opt-in roll-up for shared visibility + pooled learnings via the SaaS (separate repo). The client (this package) is **production-ready and fully standalone**. The team/org dashboard (a separate server) is **in development** — see [ROADMAP.md](docs/ROADMAP.md).

## Quickstart

**A) Set up with Claude (recommended).** Point Claude Code / the desktop app at this repo and say:
> *Install spendguard from this repo and run the guided setup in `SETUP.md`.*

Or just run `spendguard init` — it reads the **config registry** (`src/spendguard/config_schema.py` — the
single source of truth for every setting, its default, valid options, and whether it's secret) and walks you
through caps, projects, and providers **conversationally**, one question at a time, then writes your config.
Pointed at this repo, Claude does the same end-to-end: installs the package, runs the interview off that same
registry, and wires up the gate. Details: [SETUP.md](SETUP.md).

**B) pip + code.**
```
pip install llm-spendguard      # once published to PyPI
# or, from a clone of this repo:
pip install -e .
```
```python
import spendguard                    # importing the guard now GATES every OpenAI/Anthropic call in this process
# spendguard.install(cap=75)         # optional — set a per-batch cap (import already installed the gate)
```
`import spendguard` auto-installs the gate (idempotent, fail-open), so `pip install` + `import` is enough — no more
silently-ungated spend. Knobs: `SPENDGUARD_NO_AUTOINSTALL=1` opts out; `SPENDGUARD_REQUIRE=1` makes the import
**fail-closed** (raises if an SDK is present but the gate can't enforce here — refuse loudly rather than spend
ungated). For a hard guarantee in a script, keep `spendguard.require()` at the top.

Or auto-install for every process in a venv — drop this in `sitecustomize.py`:
```python
import spendguard; spendguard.install()
```
Configure with `spendguard init` (interactive — or `init --quick` for non-interactive defaults) / `spendguard
config` (show current); see [Configuration](#configuration).

## CLI — full command reference
```
# enforce / control
spendguard status | on | off                 # kill switch (persistent flag)
spendguard doctor                            # is the gate ENFORCING in THIS interpreter? (+ ledger-leak check)
spendguard install-hook --venv <path>        # gate every process in ANOTHER venv/repo (--uninstall to remove)
spendguard install-hook --user [--python P]  # gate a python's per-USER site (system-python bypass; PEP668-safe, no pip)
spendguard install-rule [--global|--project DIR]  # drop the spendguard rule into CLAUDE.md → every AI chat wires it in
spendguard install-skills                    # deploy /spend + /spendguard-learn as Claude slash-commands
spendguard coverage                          # across ALL pythons (3.11/3.14/…): which can call LLMs & which are GATED
# in code, fail-closed:  import spendguard; spendguard.require()   # refuses to run if NOT actually gated

# teams / orgs (client seam → future server repo, llmspendguard.com)
spendguard saas [status|ping|push|pull]      # opt-in roll-up; partner not supervisor; private until you enable it

# see the money
spendguard receipt [--json|--line]                    # running today/7d/month tally; auto-emitted after every flow
spendguard report [--alert-threshold 150] [--email]   # daily/weekly/monthly + ledger-leak alert + top learnings
spendguard reconcile openai|anthropic [--by-day]      # actual billed batch spend from the provider
spendguard reconcile all                              # UNIFIED view: every source (LLM+GPU) via one account-anchored loop
spendguard reconcile-ledger [--since DATE]            # local gate ledger vs provider billing → find LEAKS
spendguard calls [--intent X]                # per-intent cost + good% + $/good (opt-in corpus)
spendguard estimate --items N --from-sample f.jsonl --packs 1,30
spendguard pricing | cross-check | check-prices | sync-prices   # canonical table · OpenRouter drift · freshness · LiteLLM sync
spendguard audit [--ci]                       # fail if a script hardcodes a price ≠ the table

# plan / decide  (the briefing + advisor loop)
spendguard brief --task "..."                 # "what we need to do" → pre-filled confirm-or-correct plan
spendguard advise [--intent X] [--plan M]     # deterministic per-intent ranking by $/good (no spend)
spendguard optimize --intent X [--plan M]     # caged LLM recommendation (cheapest config that holds quality)
spendguard models [show <model>]              # per-model learnings, auto-applied (reasoning/cache/tokens)
spendguard insights list|export|import        # living insights; opt-in scrubbed collective learning
spendguard backtest --as-of DATE             # replay advise as of a past date

# prove / run cheaper  (estimate-first, caged by caps.meta)
spendguard experiment --intent X --model M... [--semantic embed|rubric] [--run]   # A/B cost↓ + same-output, graduated
spendguard promote --intent X --model M [--input chunk.jsonl] [--batch] [--run]    # run the winner + KEEP output
spendguard cache-audit | cache-test --script f.py [--run]   # prompt-caching: find + prove savings
spendguard cascade --ladder cheap,…,strong --intent X [--prompt …] --run           # cheap→verify→escalate
spendguard cache-stats | dedup --input f.jsonl --out u.jsonl | dedup-populate      # response cache + batch dedup

# work-done attribution (org → team × project), all sources
spendguard claude-code [show|sync|classify|work|story]   # mine ~/.claude → Claude Code spend + work (incremental, classified)
spendguard chat [test|show|discover|classify|loop|work|story|sync|status|accept]   # claude.ai chat adapter (OPT-IN, on-device, macOS)
spendguard resources [show|snapshot|sync|discover]   # vast.ai GPU → org/team/project (discover [--agentic] recovers destroyed boxes)

# cold start / corpus
spendguard bootstrap [--repo] [--transcripts]   # mine ALL history → corpus + insights (free, then estimate)
spendguard fetch-io [--cap 50]                  # recover real prompt+output from providers (free)
spendguard backfill [--intent-map …]            # seed corpus + graph from the batch ledgers (free)
spendguard mine-history {intents,graph,git} [--apply]   # reconstruct intents/edges from the repo (free)
spendguard mine-conv {index,synth} [--run]      # mine session transcripts for the cost playbook
spendguard validate                             # re-check learnings vs the current corpus (lifecycle)

# setup
spendguard init | config                        # guided setup / show resolved config
spendguard schedule [--daily] [--remove]        # install the OS-native scheduler (launchd/cron/schtasks)
```

### The workflow it's built around
**brief** (pre-filled plan) → **experiment** (prove the cheapest config that holds quality, graduated) →
**promote** (run it + keep the output) → the gate **enforces** caps → **reconcile-ledger** (catch leaks vs
provider billing) → **report** (daily email: totals + leak alert + top learnings) → **validate** (learnings
stay true as data grows) → those learnings feed the next **brief**.

### Gate another repo
The gate auto-installs per venv via a `sitecustomize.py` hook. To gate another project:
```
spendguard install-hook --venv /path/to/that-repo/.venv     # pip-installs spendguard + writes the hook
```
Then every process in that venv is gated (kill switch: `GATE_DISABLE=1` or `spendguard off`). Until a repo
is gated, its provider spend shows up in `reconcile-ledger` as a **leak** (billed but ungoverned).

### Enforce the gate on remote / distributed compute (vast.ai, any SSH host)
The gate only governs the interpreter it's loaded in — a freshly-spun-up box's `python3` is **ungated** until it's
provisioned, so remote LLM scripts can spend silently. Make it structural — *gate at provision, verify before spend,
sync before teardown*:
```
spendguard remote onstart                              # boot snippet → bake into the instance onstart (gates every python3)
spendguard remote verify --ssh "ssh -p PORT root@HOST -i KEY"   # FAIL-CLOSED: exit≠0 if the box isn't ENFORCING → abort the launch
spendguard remote sync   --ssh "ssh -p PORT root@HOST -i KEY" --project manga2anime   # roll the box ledger up to the org (idempotent)
```
On the box itself, an LLM script should also `import spendguard; spendguard.require()` (fail-closed in-process). Then
an ungated box can't spend: provisioning gates it, `verify` refuses to launch if it didn't, `require()` aborts the
script, and `sync` attributes the spend before the ephemeral box is destroyed.

### Always-on spend tally (inline receipts)
After every gated **flow** — a `with spendguard.context(intent=…): …` block, a batch submit at the gate, or a CLI
command — spendguard prints a compact receipt so what it tracked is visible the moment it happens:
```
spendguard ▸ loinc-typing · 42 calls · in 1.2M / out 300.0K · est $2.10 → actual $1.87 (−11%)
             actual-$ (billed): today $81 · 7d $421 · month $2,015
             est-value (plan, not billed) (as of 2026-06-23): today $1.4k · 7d $8.6k · month $20.2k
```
The two axes are always kept **separate and never summed**: **actual-$** is money billed (the gate ledger, reconciles
to provider truth); **est-value** is coding-agent usage *value* — **Claude Code + claude.ai + Codex** (what it would
cost at API rates — covered by your plan), stamped per-source so they sum. It's per-FLOW (not per-call), costs nothing
(a local read, no LLM, no admin key), and the verbosity is `receipts.level` / `SPENDGUARD_RECEIPTS` =
`off | footer | flow | verbose` (default `flow`). Check it any time:
```
spendguard receipt            # the two-line tally   ·   --line = one compact line   ·   --json = machine-readable
```

**Surface it in your Claude Code chat** — one command (idempotent; backs up + can `--remove`):
```
spendguard install-receipts --host claude-code      # adds a statusLine footer + a per-turn transcript notice
```
It registers two guarded hook protocols in `~/.claude/settings.json`: `receipt --statusline` (always-on footer:
`cwd · model · ctx% · tally`) and `receipt --stop-hook` (a `systemMessage` line each turn). A hook can never block or
break a turn. Restart Claude Code to apply.

**Other hosts (Codex, editors, menubar).** Codex has no in-chat hook, but spendguard still TRACKS it
(`spendguard codex show` → channel=codex, billed=false). To surface the tally anywhere, point a **sink** at a file
and render that: `receipts.sinks` / `SPENDGUARD_RECEIPTS_SINK` = `stderr` (default) | `stdout` | `file:<path>`
(comma-separated). e.g. `spendguard config set receipts.sinks 'stderr,file:~/.spendguard/receipt.log'`, then
`tail -f ~/.spendguard/receipt.log` in a pane.

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

## Configuration
Everything lives in **two files** under `~/.spendguard/` (both scaffolded by `spendguard init`); a real
environment variable always overrides either, so prod / CI / secret-managers Just Work. `spendguard config`
prints the resolved value + source for every setting.

**① `keys.env` — secrets** (chmod 600). Loaded into the environment on `import spendguard`, so your **own**
`openai.OpenAI()` / `anthropic.Anthropic()` calls pick the keys up too — no separate export needed. Fill only the
blanks you use:
```bash
OPENAI_API_KEY=            # + ANTHROPIC_API_KEY / GEMINI_API_KEY / DEEPSEEK_API_KEY / DASHSCOPE_API_KEY
VAST_API_KEY=              # remote GPU compute (vast.ai), metered into the same ledger
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
Built-in providers: **openai, anthropic, gemini, deepseek, qwen** (most via their OpenAI-compatible
endpoints, so the gate already meters them). Keys resolve per provider from env / `~/.spendguard` / `./.env`
(`OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, `GEMINI_API_KEY`, `DEEPSEEK_API_KEY`, `DASHSCOPE_API_KEY` for Qwen).
**Add another in one line:**
```python
from spendguard.adapters import register_provider
register_provider("together", "https://api.together.xyz/v1", "TOGETHER_API_KEY", ("meta-llama", "mistralai"))
```

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

## Safety
Fail-**open**: any estimation or patch error logs a warning and lets the call proceed — the gate
never breaks a job by accident. Only the deliberate over-cap stop blocks. Disable instantly with
`spendguard off` (checked per-call, live) — and the kill switch is honored even if the gate itself errors.

## Getting help
- **Website:** https://llmspendguard.com
- **Bugs / feature requests:** [GitHub Issues](https://github.com/llmspendguard/llm-spendguard/issues)
- **Questions / ideas / show-and-tell:** [GitHub Discussions](https://github.com/llmspendguard/llm-spendguard/discussions)
- **Contributing:** see [CONTRIBUTING.md](CONTRIBUTING.md).

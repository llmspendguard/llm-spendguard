# spendguard setup

Two ways to configure spendguard. `pip install` puts only the **code** in site-packages; your **config + data**
live under **`~/.spendguard/`** (override with `SPENDGUARD_HOME`), created by `spendguard init` — never by pip. You
edit **two files**: non-secret settings in `config.json`, and your keys (LLM · vast.ai · org) in `keys.env` — which
spendguard loads into the environment on `import`, so your own `openai` / `anthropic` clients see them too. A real
environment variable always wins. (Full file map: README → Configuration.)

---

## Option A — guided by Claude (recommended)

Point Claude (Claude Code / the desktop app) at this repo and paste:

> Install the spendguard package from this repo (`pip install -e .`), then run the guided setup:
> read `src/spendguard/config_schema.py`, and for each setting ask me one question at a time with its
> default and valid options, then write my answers to `~/.spendguard/config.json` and
> `~/.spendguard/email.json`, and scaffold `~/.spendguard/keys.env` with a blank placeholder per secret key
> (real keys go in keys.env or the environment — never in config.json).
> Finally enable the gate by adding `import spendguard; spendguard.install()` to the venv `sitecustomize.py`,
> and run `spendguard config` and `spendguard sync-prices` to confirm.

Claude reads the **config registry** (`config_schema.SETTINGS`) — the single source of truth for every
knob, its default, valid options, and whether it's a secret — so it always asks about exactly the
settings the code actually has. That registry is what makes this self-describing.

## Option B — do it yourself

```bash
pip install -e .            # or: pip install spendguard
spendguard init            # interactive: writes config.json + scaffolds keys.env (placeholders to fill)
spendguard config          # show resolved settings + where each came from
spendguard sync-prices     # cache LiteLLM prices (breadth + freshness)
```
Then enable the gate for every process in your venv — add to `.../site-packages/sitecustomize.py`:
```python
import spendguard; spendguard.install()
```
or call `spendguard.install()` at your app's entry point.

---

## What you'll be asked (the ~7 decisions, all with defaults)

| Setting | Default | Notes |
|---|---|---|
| `caps.per_batch` | **75** | Hard-stop any single batch projected over this many $. |
| `caps.realtime` | **50** | Cumulative real-time $ cap (per process, or fleet-wide with sqlite). |
| `caps.meta` | **2.0** | Daily $ cap for spendguard's OWN advisor LLM use (intent `spendguard:*`) — separate from workload caps. |
| `caps.total.{daily,monthly}` | off (`null`) | Overall spend ceiling ($), LLM + remote-compute. Legacy flat `caps.daily`/`caps.monthly` still honored as this total. |
| `caps.llm.{daily,monthly}` | off (`null`) | LLM (OpenAI+Anthropic) sub-cap ($) — **HARD, gate-enforced**. |
| `caps.compute.{daily,monthly}` | off (`null`) | Remote-compute (vast.ai GPU) sub-cap ($) — **alert/soft** (launches don't hit the gate). |
| `gate.enforce` | **warn** | Test-first rail for big batches: `off` / `warn` / `block` (the estimate→test→run sequence). |
| `budget.backend` | **memory** | `sqlite` for cross-process daily/monthly caps (a shared ledger). Required for all `caps.{total,llm,compute}.*`. |
| `budget.db_path` | `<home>/spend.db` | Where the SQLite ledger lives. |
| `deid.engine` | **regex** | Redact PII/PHI from text that leaves on sync paths: `regex` (floor, zero deps) / `presidio` (floor + NER) / `off`. |
| `emit.webhook` / `emit.otel` | off | Send each gated event to a webhook / OpenTelemetry. |
| `email.provider` (+ `to`, `from_`, key) | off | Daily report delivery (resend or smtp). |
| `saas.*` | off (`enabled=false`) | Team/org roll-up client seam: `enabled`, `url`, `api_key` (Bearer, secret), `visibility` (`private`/`team`/`org`), `sync_interval`, `contributor`, `project`. Off until you connect a server. |
| API keys | `keys.env` or env | `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, `GEMINI_API_KEY`, `DEEPSEEK_API_KEY`, `DASHSCOPE_API_KEY`, `ZAI_API_KEY`, `VAST_API_KEY`, `SPENDGUARD_SAAS_KEY`. |
| `advisor.executor` | **api** | Where spendguard's own meta prompts run: `api` (metered, caps.meta) / `claude-code` (Anthropic plan) / `codex` (ChatGPT plan) / `pool` (both lanes, each serving its own provider; failures cool the lane and fall back to the API). Activation status prints at the end of `init` and in `doctor`; verify live with `spendguard lanes --probe` (one tiny plan-billed prompt per lane, $0). |
| `key_profile` (repo `.spendguard.json`) | off | Per-repo key selection: keys.env holds `<VAR>__<profile>` entries (e.g. `ANTHROPIC_API_KEY__lmm=…`); the repo's profile picks them. Real env always wins. |

Everything is optional — with nothing configured, the gate still runs with the $75 per-batch cap and
prints the report locally. Tune anytime by re-running `spendguard init` or editing `~/.spendguard/config.json`.
The full registry (every setting, default, valid options, secret-or-not) is `src/spendguard/config_schema.py`.

## Per-repo keys (workspace/project scoping) + per-key spend

One global `~/.spendguard/keys.env` can hold every workspace/project-scoped key:

```
ANTHROPIC_API_KEY=sk-ant-default…            # default for repos with no profile
ANTHROPIC_API_KEY__lmm=sk-ant-lmm-ws…        # the lmm repo's Anthropic WORKSPACE key
OPENAI_API_KEY__lmm=sk-proj-lmm…             # the lmm repo's OpenAI PROJECT key
```

and each repo's `.spendguard.json` picks its own with `"key_profile": "lmm"` (or set
`SPENDGUARD_KEY_PROFILE`). A real environment variable always beats both. Pair the profile with
provider-side scoping — an OpenAI **project key** and an Anthropic **workspace key** per repo — and the
provider's own billing then splits per repo, so reconcile cross-checks each repo against its own
workspace truth instead of the shared account total. Every charge is stamped with the serving key's
fingerprint (`sha256[:8]:last4`, local-only, never pushed); `spendguard keys` shows $ per key.

# spendguard setup

Two ways to configure spendguard. Both write to `~/.spendguard/` (config + secrets, gitignored);
API keys stay in your environment / `.env`.

---

## Option A — guided by Claude (recommended)

Point Claude (Claude Code / the desktop app) at this repo and paste:

> Install the spendguard package from this repo (`pip install -e .`), then run the guided setup:
> read `src/spendguard/config_schema.py`, and for each setting ask me one question at a time with its
> default and valid options, then write my answers to `~/.spendguard/config.json` and
> `~/.spendguard/email.json` (never put API keys in those files — tell me which env vars to set).
> Finally enable the gate by adding `import spendguard; spendguard.install()` to the venv `sitecustomize.py`,
> and run `spendguard config` and `spendguard sync-prices` to confirm.

Claude reads the **config registry** (`config_schema.SETTINGS`) — the single source of truth for every
knob, its default, valid options, and whether it's a secret — so it always asks about exactly the
settings the code actually has. That registry is what makes this self-describing.

## Option B — do it yourself

```bash
pip install -e .            # or: pip install spendguard
spendguard init            # interactive: writes ~/.spendguard/config.json (+ email.json)
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
| `caps.daily` / `caps.monthly` | off | Cross-process spend caps ($). Need `budget.backend = sqlite`. |
| `budget.backend` | **memory** | `sqlite` for cross-process daily/monthly caps (a shared ledger). |
| `budget.db_path` | `<home>/spend.db` | Where the SQLite ledger lives. |
| `emit.webhook` / `emit.otel` | off | Send each gated event to a webhook / OpenTelemetry. |
| `email.provider` (+ `to`, `from_`, key) | off | Daily report delivery (resend or smtp). |
| API keys | from env | `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, `GEMINI_API_KEY`, `DEEPSEEK_API_KEY`, `DASHSCOPE_API_KEY`. |

Everything is optional — with nothing configured, the gate still runs with the $75 per-batch cap and
prints the report locally. Tune anytime by re-running `spendguard init` or editing `~/.spendguard/config.json`.

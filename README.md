# llm-spendguard

**Know what an LLM job will cost before you run it — and prove your ledger matches the provider's bill.**

```bash
uvx --from llm-spendguard spendguard scan        # 10 seconds. No key, no config, nothing leaves your machine.
```

That reads the coding-agent transcripts already on your disk and tells you what that work costs at API rates.
Nothing is installed into your interpreter and nothing is uploaded — see [what leaves your
machine](#what-leaves-your-machine).

Then, when you want the guard rails:

```bash
spendguard run -- python your_job.py   # gate ONE command: cost estimate before submit + hard caps
spendguard reconcile all               # your ledger vs the provider's actual bill, with the gap NAMED
```

Four things it does that the observability and gateway tools don't:

1. **Estimates a batch before you submit it** — not after the tokens are billed.
2. **Refuses to price a model it doesn't know**, loudly, instead of logging $0. (Elsewhere a $0 price can also
   silently exempt the call from your budget cap — an unknown model becomes an *uncapped* model.)
3. **Reconciles to the actual invoice** and names the residual as ungoverned spend, with a published error rate —
   see **[ACCURACY.md](ACCURACY.md)**.
4. **Learns the cheaper config and proves it's safe** before adopting it (measure → test for output equivalence →
   adopt only if quality holds → apply → remember what failed).

Zero required dependencies. Fail-open by design: a cost tool must never be the reason your job doesn't run.
Learn more at https://llmspendguard.com · **[Docs & quickstart →](https://docs.llmspendguard.com/)**

> 📘 **New here? Read the [Solution Specification](docs/SOLUTION-SPEC.md)** — the whole story end to end: why it
> exists, the value, the journey of a dollar (call → gate → ledger → reconcile → push), the design, and how it's
> tested, secured, and operated.

## What leaves your machine

Three modes. **The default is local-only** — you have to opt in to each of the other two.

| Mode | What leaves | How to turn it on |
|---|---|---|
| **Local-only (default)** | **Nothing.** `scan`, `run`, the gate, the ledger, `reconcile`, `trust` all work with no network except the providers' own free billing endpoints, which send *nothing* — they only read your usage. | — (this is the default) |
| **LLM attribution** (opt-in) | Excerpts of local coding-agent transcripts go to a model provider so an LLM can classify which project/team spend belongs to. De-identified first (`deid.engine`). | you run `spendguard chat …` / `accounting --run` |
| **Team dashboard** (opt-in) | Per-day, per-model **roll-ups only**: day, provider, model, kind, project, $ and token counts. **No prompts, no outputs, no keys.** | you configure `saas.json` and run `saas sync` |

Two things worth stating plainly, because they are the questions a careful reviewer asks first:

- **Your API keys never leave the machine and are never sent to our server.** Provider billing is read locally with
  your key; only the resulting numbers are pushed, and only if you connect a team dashboard.
- **Coding-agent transcripts can contain secrets** (a `.env` echoed into a session, a pasted credential). That is
  exactly why LLM attribution is opt-in and de-identified rather than on by default — and why `scan`, the command
  we ask you to run first, never sends them anywhere.

`spendguard run --show` prints the exact bootstrap that will execute in your process. Nothing is downloaded at
runtime, ever.

<details>
<summary><b>The <code>chat</code> extra reads your claude.ai session — here is exactly what it does and why</b></summary>

The claude.ai desktop app caches **no** conversations on disk, so the only way to account for chat usage is
claude.ai's own API, authenticated with the `sessionKey` cookie your browser already holds. That is why the
optional `chat` extra decrypts a cookie — and we understand how that phrase reads in 2026, so here is the whole
of it:

- **It is opt-in twice**: you must install the `chat` extra *and* set `chat.enabled` (or `SPENDGUARD_CHAT_ENABLED`).
  Nothing happens by default; `scan`, the gate, the ledger and reconcile never touch it.
- **What it reads**: `~/Library/Application Support/Claude/Cookies` (the app's own Chromium cookie DB) and only
  the entries `sessionKey`, `lastActiveOrg`, `cf_clearance`. The AES key comes from **your** macOS Keychain, which
  prompts *you* for permission — spendguard cannot bypass that prompt, and you can deny it.
- **Where the token goes**: nowhere. It authenticates requests to claude.ai from your machine. It is never logged,
  never printed, never sent to our server, and never included in a roll-up. The decrypt happens in-process
  (the `cryptography` dep exists precisely so the key is *not* passed on a command line where `ps` could see it).
- **What is cached**: the cookie, `chmod 0600`, with a TTL (`chat.cookie_ttl_h`, default 12h), cleared on any 401.
  Delete `~/.spendguard/` to purge it.
- **What is pushed**: if — and only if — you connect a team dashboard, the same per-day roll-ups as everything
  else. No conversation text, no titles, no token.
- **Don't want any of that?** Skip the extra entirely. You keep the gate, the ledger, reconcile, `scan` and
  Claude Code / Codex accounting; you lose only claude.ai chat value. Or set the session key yourself and no
  cookie is ever read.

Why it's worth having: chat is where a large share of plan-covered work actually happens, and leaving it out
doesn't make the spend disappear — it makes your total quietly wrong, which is the one thing this project exists
to prevent.

</details>

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
- **gate** — overlay on the OpenAI/Anthropic SDKs (armed by `spendguard run --`, by `import spendguard`, or venv-wide via the opt-in hook): estimates every
  batch/real-time call — chat, Responses API, Anthropic messages, **and embeddings** (realtime + batch
  `input` bodies) — **hard-stops** over a cap (per-batch + cross-process daily/monthly) — then *asks* if interactive.
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

**Use with an AI assistant:** `spendguard install-rule --global` writes a rule into `CLAUDE.md` so **every** Claude/Cursor conversation routes the LLM code it builds through spendguard — then `spendguard install-skills` adds the slash-commands: `/spend` (status), `/spendguard-reconcile` (trust the number), `/spendguard-learn` (advisor), `/spendguard-prompts` (the prompt lab), `/spendguard-close` (monthly close). See [Use with Claude](docs/USING-WITH-CLAUDE.md).
**Teams & orgs:** each user keeps their own ledger + sets their own caps (partner, not supervisor); opt-in roll-up for shared visibility + pooled learnings via the SaaS (separate repo). The client (this package) is **production-ready and fully standalone**. The team/org dashboard is **live** at [llmspendguard.com](https://llmspendguard.com) — see [ROADMAP.md](docs/ROADMAP.md).

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
pip install llm-spendguard
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

To gate **one command** without touching your interpreter at all — the default, and the shape `ddtrace-run` and
`opentelemetry-instrument` use:
```bash
spendguard run -- python your_job.py
spendguard run --show          # print the exact bootstrap that will execute, before you trust it
```
Nothing is written into site-packages and nothing persists once the process exits. To arm **every** process in a
venv you own, that's an explicit opt-in: `spendguard install-hook --venv .venv` (remove with `--uninstall`).
We don't default to writing startup hooks — see [Architecture](docs/ARCHITECTURE.md) for why.
Configure with `spendguard init` (interactive — or `init --quick` for non-interactive defaults) / `spendguard
config` (show current); see the [configuration reference](docs/REFERENCE.md#configuration).

## Where to go next

| | |
|---|---|
| **[Full CLI reference](docs/CLI.md)** | Every command, grouped: enforce · see the money · plan · prove · attribute · setup. |
| **[Configuration & subsystem reference](docs/REFERENCE.md)** | Env knobs, caps by class, pricing layers, the learned estimator, the advisor, observability, de-identification. |
| **[Accuracy](ACCURACY.md)** | How close these numbers are to your real invoice — and what we do **not** capture. |
| **[Architecture](docs/ARCHITECTURE.md)** | The gate chokepoint and the extensibility seams. |
| **[Use with Claude / Cursor](docs/USING-WITH-CLAUDE.md)** | Make every assistant session gated; the slash-commands. |
| **[Solution spec](docs/SOLUTION-SPEC.md)** | The whole story end to end: why, value, the journey of a dollar, design, ops. |
| **[Setup](SETUP.md)** · **[Changelog](CHANGELOG.md)** · **[Contributing](CONTRIBUTING.md)** · **[Security](SECURITY.md)** | |

## Safety
Fail-**open**: any estimation or patch error logs a warning and lets the call proceed — the gate
never breaks a job by accident. Only the deliberate over-cap stop blocks. Disable instantly with
`spendguard off` (checked per-call, live) — and the kill switch is honored even if the gate itself errors.

## Getting help
- **Website:** https://llmspendguard.com
- **Bugs / feature requests:** [GitHub Issues](https://github.com/llmspendguard/llm-spendguard/issues)
- **Questions / ideas / show-and-tell:** [GitHub Discussions](https://github.com/llmspendguard/llm-spendguard/discussions)
- **Contributing:** see [CONTRIBUTING.md](CONTRIBUTING.md).

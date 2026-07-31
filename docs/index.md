# llm-spendguard

A **pre-spend governor** for LLM and remote-compute cost. It caps every call *before* the spend, prices from a
verified table, reconciles against actual provider billing, and **learns the cheapest config that still holds
quality** — then proves and enforces it.

Zero required dependencies. One-line install. It **never breaks your job** — over a cap it asks (interactive)
or fails *open* with a logged warning (non-interactive), never a crash.

<div class="grid cards" markdown>

- :material-book-open-variant: **[Solution Specification](SOLUTION-SPEC.md)** — the whole story end to end (start here).
- :material-shield-check: **[Architecting Win](spendguard_architecting_win.md)** — the enterprise quality playbook: checklists, processes & LLM prompts.
- :material-rocket-launch: **[60-second quickstart](#quickstart)** — install, gate a call, see it work.
- :material-sitemap: **[Architecture](ARCHITECTURE.md)** — the gate chokepoint + the extensibility seams.
- :material-robot: **[Using with Claude & agents](USING-WITH-CLAUDE.md)** — make every assistant session gated.
- :material-brain: **[Learning advisor](learning-advisor.md)** — recommend *considering* history, not parroting it.
- :material-scale-balance: **[Accuracy](ACCURACY.md)** — how close these numbers are to your actual invoice, and
  what we do **not** capture. Nobody else in this category publishes one.

</div>

---

## Why

Cost overruns don't announce themselves. They slip in quietly: a hardcoded price that drifted from the real
rate, a forgotten model swap, under-batching that re-bills a shared prompt on every request, a job cancelled
"to save money" that still bills for the work already completed, an ungated script in *some other venv* leaking
spend nobody is watching.

spendguard stops those *before* the money moves — and then tells you, with evidence, the cheaper way to do the
same work without losing quality.

!!! note "Your keys, your data"
    spendguard runs **in your process** with **your** API keys. It never proxies or resells tokens. State lives
    locally under `$SPENDGUARD_HOME` (`~/.spendguard` by default). The optional team roll-up sends only
    **scrubbed aggregates** (daily totals + generalizable learnings) — never prompts, outputs, or keys.

---

## Quickstart

### 1. See something real before you install anything

```bash
uvx --from llm-spendguard spendguard scan
```

Reads the Claude Code / Codex transcripts already on your disk and prints what that work costs at API rates.
**No key, no config, no network, nothing leaves your machine** — about ten seconds. If you like what it shows,
keep going.

### 2. Install

```bash
pip install llm-spendguard
```

### 3. Gate a command

The default way to govern a job is a **wrapper** — it puts the gate on that one command's `PYTHONPATH` and execs
it, exactly like `ddtrace-run` or `opentelemetry-instrument`:

```bash
spendguard run -- python train.py      # estimated + capped before it spends; nothing installed into your venv
spendguard run --show                  # prints the exact bootstrap that will execute — read every byte
```

Nothing is written into site-packages, nothing persists after the process exits, and not using the wrapper is the
complete uninstall. (We default to this on purpose: writing `sitecustomize`/`.pth` startup hooks into someone's
interpreter is the mechanism that shipped a credential stealer in another package in March 2026. See
[Architecture](ARCHITECTURE.md).)

In your own code, one line does the same for that process:

```python
import spendguard          # importing it arms the gate for this process (idempotent, fail-open)
```

Want it on for **every** process in a venv you own? That's still supported and is now an explicit opt-in:
`spendguard install-hook --venv .venv` — and `spendguard install-hook --venv .venv --uninstall` removes it.

Or let Claude (or any agent) set it up conversationally, picking your caps, projects, and providers — and
optionally connecting a team:

```bash
spendguard init
```

Verify the gate is actually live in *this* interpreter (it's per-interpreter — a different venv is not gated):

```bash
spendguard doctor        # prints ENFORCING HERE: YES when the gate is loaded and active
```

### 3. Spend normally — it just governs

Write your normal code. spendguard sits in front of every paid call:

```python
from openai import OpenAI
client = OpenAI()

resp = client.chat.completions.create(            # ← estimated, priced, and checked against your caps first
    model="gpt-5.5",
    messages=[{"role": "user", "content": "Summarize this ticket."}],
)
```

- **Under cap** → the call runs, the spend is recorded to the local ledger, and pricing comes from the canonical
  table (never a hardcoded constant).
- **Over cap** → interactive sessions get a prompt (proceed / skip); non-interactive runs log a warning and
  **fail open** so a batch job is never silently killed mid-flight.

Want a hard stop instead of fail-open for a critical script? Assert the gate is enforcing up front:

```python
import spendguard
spendguard.require()      # raises if the gate isn't actually enforcing here — a bypass can't run silently
```

### 4. See what you spent — and what leaked

```bash
spendguard report                 # daily / weekly / monthly, per provider, + a ledger-vs-reality leak alert
spendguard reconcile openai       # local ledger vs ACTUAL provider billing → surfaces ungoverned spend
spendguard reconcile anthropic
```

`reconcile` is the honesty check: it pulls real usage from the provider and diffs it against what the gate saw.
A gap means spend escaped the gate (an ungated venv, a different machine) — exactly the leak you want to find.

### 5. Set caps that matter

Caps are split so you can govern each kind of spend independently — and a single **total** ceiling over
everything:

```bash
spendguard config set caps.llm.daily 25         # LLM/embeddings: $25/day
spendguard config set caps.compute.monthly 400  # remote compute (e.g. vast.ai GPUs): $400/month
spendguard config set caps.total.daily 60       # everything combined: $60/day
```

Before any **paid batch**, do a separate zero-spend estimate, confirm, then submit:

```bash
spendguard estimate --items 12000 --model gpt-5.5 --in-tokens 800 --out-tokens 300
```

### 6. (Optional) roll up a team

Used solo, everything above is fully local. To see an org's combined spend, leaks, and learnings at
[llmspendguard.com](https://llmspendguard.com):

```bash
spendguard saas link              # shows a code, you approve it in the browser → your verified email is the contributor
```

From then on, each contributor's **scrubbed daily aggregates** roll up under the org. Billing is by **active
contributors that month** (free ≤ 2), and the team sees combined spend, governance coverage, and the shared
**learnings** — the cheapest-config rules one teammate proved, now available to everyone.

---

## What it gives you

- **Correct prices, always** — one canonical table, layered + cross-checked, never hardcoded; an `audit` enforces it.
- **Estimate before spend** — every paid path projects cost first; the gate hard-stops over caps (asks, if interactive).
- **Cost-per-*good*-result** — a cheap call that fails quality is 100% waste, so the metric is `$/good`, and any model/format downgrade is **quality-gated** (proven by `experiment`, not assumed).
- **The governor is caged** — the advisor's own LLM use has a separate `caps.meta` budget and is excluded from the corpus it analyzes, so it can't overspend or pollute its own learning.
- **Living, validated learnings** — insights are conditional rules with a confidence + lifecycle, re-validated as data grows, and shareable (scrubbed) across a team.
- **Self-contained & non-blocking** — zero required deps, fail-open, state isolated under `$SPENDGUARD_HOME`; observability is exported (OTel), not another dashboard to babysit.

## Where to next

- **[Architecture](ARCHITECTURE.md)** — how the gate, pricing resolution, the learning loop, and the meta-cage fit together (with diagrams), plus honest known limitations.
- **[Using with Claude & agents](USING-WITH-CLAUDE.md)** — wire spendguard into *every* assistant session with a standing `CLAUDE.md` rule and slash-commands.
- **[Learning advisor](learning-advisor.md)** — the corpus → insights → temporal graph, the deterministic / caged-LLM split, and the meta-budget cage.
- **[Roadmap](ROADMAP.md)** — what's shipped and what's next.

The full command reference lives in the [project README](https://github.com/llmspendguard/llm-spendguard#readme).

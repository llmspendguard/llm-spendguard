# llm-spendguard

A **pre-spend governor** for LLM and remote-compute cost. It caps every call *before* the spend, prices from a
verified table, reconciles against actual provider billing, and **learns the cheapest config that still holds
quality** — then proves and enforces it.

Zero required dependencies. One-line install. It **never breaks your job** — over a cap it asks (interactive)
or fails *open* with a logged warning (non-interactive), never a crash.

<div class="grid cards" markdown>

- :material-rocket-launch: **[60-second quickstart](#quickstart)** — install, gate a call, see it work.
- :material-sitemap: **[Architecture](ARCHITECTURE.md)** — the gate chokepoint + the extensibility seams.
- :material-robot: **[Using with Claude & agents](USING-WITH-CLAUDE.md)** — make every assistant session gated.
- :material-brain: **[Learning advisor](learning-advisor.md)** — recommend *considering* history, not parroting it.

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

### 1. Install

```bash
pip install llm-spendguard
```

### 2. Turn on the gate

Two lines. Every OpenAI / Anthropic call in this process is now **estimated and capped before it spends**:

```python
import spendguard
spendguard.install()      # patches the OpenAI + Anthropic clients in-process; safe to call once at startup
```

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

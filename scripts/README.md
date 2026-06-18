# `scripts/`

Helper scripts that live alongside the package but aren't part of the importable library.

> The team/org dashboard these scripts push to is a **separate server repo, currently in development**.
> The client (this package) is production-ready and works standalone — see [docs/ROADMAP.md](../docs/ROADMAP.md).
> Learn more at https://llmspendguard.com.

## `bootstrap-remote.sh`

**What it does.** Self-configures spendguard on a **remote / ephemeral GPU box** (e.g. a vast.ai H200) so the
LLM/API calls that run there are **gated**, **attributed** to the right org/project, and **pushed** to the
aggregation server. In one run it:

1. Installs the package (`pip install llm-spendguard`, falling back to an editable install from a synced
   checkout at `$SPENDGUARD_SRC` / `~/llm-spendguard`).
2. Writes provider keys to the cwd-independent home (`~/.spendguard/.env`, `chmod 600`) so `reconcile`/`report`
   resolve them from any directory.
3. Writes a per-repo connection (`./.spendguard.json`, `chmod 600`) that pushes **this box's project** to its
   org via the org/team ingest key — `enabled`, `url`, `contributor`, `project`, `visibility: org`, daily sync.
4. Gates the interpreter via `spendguard install-hook --user --python "$(command -v python3)"` so every call is
   recorded (tagged `project=$PROJECT`).
5. **Self-verifies** with `spendguard doctor` (must show the gate ENFORCING + keys resolved + the push project).

**When to use it.** Bake it into a remote GPU box's provisioning. Ephemeral boxes start clean → ungated → their
provider spend leaks (account-billed but never captured locally). This makes every fresh box configure and
verify itself on boot. It is **push-only** by design: a remote box runs `spendguard saas push`, **not**
`saas reconcile` — account-level reconciliation runs from ONE designated machine so the account gap isn't
re-attributed on every box.

**Prerequisites.**
- `bash`, `python3`, `pip`, and network access on the box.
- Secrets passed via the **environment** at provision time (never hardcoded / committed):
  - `SPENDGUARD_SAAS_KEY` — **required**; the org/team ingest key (e.g. `sg_org_…`).
  - `OPENAI_API_KEY`, `ANTHROPIC_API_KEY` — provider keys (for `reconcile` / `report`); optional but
    recommended.
- Optional overrides: `PROJECT` (default `vision-pipeline`), `CONTRIBUTOR` (default `you@example.com`),
  `SERVER_URL`, `SPENDGUARD_SRC` (editable-install source path).

> The default `PROJECT`, `CONTRIBUTOR`, and `SERVER_URL` are placeholder examples — set them for your own
> org/project/box.

**Example.**
```bash
SPENDGUARD_SAAS_KEY=sg_org_xxx \
OPENAI_API_KEY=sk-... \
ANTHROPIC_API_KEY=sk-ant-... \
PROJECT=my-project \
CONTRIBUTOR=you@your-org.com \
SERVER_URL=https://your-spendguard-server.example.com \
  ./scripts/bootstrap-remote.sh

# then, after a training/inference run on the box:
spendguard saas push        # push this box's gate-attributed spend (do NOT 'saas reconcile' on remote boxes)
```

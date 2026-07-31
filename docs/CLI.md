# CLI — full command reference

Every command `spendguard` exposes. New here? Start with the
[60-second quickstart](index.md#quickstart) — `spendguard scan` needs none of this.

## CLI — full command reference
```
# enforce / control
spendguard status | on | off                 # kill switch (persistent flag)
spendguard doctor [--live]                   # is the gate ENFORCING here? + CACHED leak verdict w/ age (--live = full pull)
spendguard install-hook --venv <path>        # gate every process in ANOTHER venv/repo (--uninstall to remove; alias: gate-venv)
spendguard install-hook --user [--python P]  # gate a python's per-USER site (system-python bypass; PEP668-safe, no pip)
spendguard install-rule [--global|--project DIR]  # drop the spendguard rule into CLAUDE.md → every AI chat wires it in
spendguard install-skills                    # deploy the 5 slash-commands (/spend, /spendguard-{reconcile,learn,prompts,close})
spendguard install-receipts --host claude-code|codex   # surface the always-on tally in a host (statusline + per-turn)
spendguard coverage                          # which LLM-calling VENVS aren't gated (ungated realtime spend sources)
spendguard gate-coverage                     # per-INTERPRETER gate check across EVERY python on the machine (3.11/3.14/…)
spendguard remote onstart|verify|sync        # enforce the gate on remote/distributed compute (vast.ai / any SSH host)
# in code, fail-closed:  import spendguard; spendguard.require()   # refuses to run if NOT actually gated

# teams / orgs (client seam → future server repo, llmspendguard.com)
spendguard saas [status|ping|link|push|pull|sync|reconcile|audit|crosscheck|commands]
#   status/ping — connection · link — device-link (approve in browser → verified email = contributor)
#   sync [--if-due] — roll-up push on cadence · push [--dry] — force now · pull — fetch pooled learnings
#   reconcile / audit / crosscheck — reconcile local ledger to provider truth, completeness audit, local↔server row diff (all free)
#   commands — drain + run server-enqueued work (reconcile / re-tag).  Opt-in; private until you enable it.

# see the money
spendguard receipt [--json|--line]                    # running today/7d/month tally; auto-emitted after every flow
spendguard report [--alert-threshold 150] [--email]   # daily/weekly/monthly + ledger-leak alert + top learnings
spendguard reconcile openai|anthropic [--by-day]      # actual billed batch spend from the provider
spendguard reconcile all                              # UNIFIED view: every source (LLM+GPU) via one account-anchored loop
spendguard reconcile-ledger [--since DATE]            # local gate ledger vs provider billing → find LEAKS (aliases: ledger-sync, leaks)
spendguard trust                                      # provider billing vs recorded — the daily double-count guard (alias: trust-check)
spendguard truth [--push]                             # per-day provider-truth totals (owner connection only) → the org statement's yardstick
spendguard close [--month YYYY-MM] [--csv] [--account]  # monthly close, client view; --account = shared-account axis (truth is account-wide)
spendguard calls [--intent X]                # per-intent cost + good% + $/good (opt-in corpus)
spendguard prompts [--intent X] [--json]     # prompt-efficiency lint: boilerplate/context/truncation/model-mix, ranked by $ at stake
spendguard realized [--intent X] [--sync]    # MEASURED before/after $/call around insight adoptions (no counterfactuals); --sync → guarded
spendguard estimate --items N --from-sample f.jsonl --packs 1,30 [--label X]   # --label adds the LEARNED correction
spendguard calibrate predict --label X --n N --model M [--transport batch] [--in-tokens T] [--out-max T]
                                             # LEARNED estimator: your captured history corrects the naive $
spendguard calibrate show | pair | backtest  # what's learned + confidence · join predictions↔actuals · MAPE vs naive
spendguard maxtokens <sig> [current_max]     # data-driven max_tokens bound for a call-class (p99×1.5 — measured, not guessed)
spendguard pricing | providers               # canonical price table · configured providers→models
spendguard cross-check | check-prices | sync-prices | refresh-prices   # OpenRouter drift · freshness · LiteLLM sync · refresh
spendguard audit [--ci]                       # fail if a script hardcodes a price ≠ the table
spendguard compare --prompt "..." --models a,b,c --show   # one prompt across providers → cost + latency + output

# plan / decide  (the briefing + advisor loop)
spendguard brief --task "..."                 # "what we need to do" → pre-filled confirm-or-correct plan
spendguard advise [--intent X] [--plan M]     # deterministic per-intent ranking by $/good (no spend)
spendguard backtest --as-of DATE              # replay advise as of a past date
spendguard optimize --intent X [--plan M]     # caged LLM recommendation (cheapest config that holds quality)
spendguard mine                               # caged: synthesize confidence-scored insights + graph from the evidence
spendguard reconstruct                        # caged: judge recovered call I/O → real good% / $/good
spendguard review                             # caged: practice audit (was the usage SMART, not just what it cost)
spendguard models [show <model>]              # per-model learnings, auto-applied (reasoning/cache/tokens)
spendguard insights list|export|import        # living insights; opt-in scrubbed collective learning

# prove / run cheaper  (estimate-first, caged by caps.meta)
spendguard experiment --intent X --model M... [--semantic embed|rubric] [--run]   # A/B cost↓ + same-output, graduated
spendguard promote --intent X --model M [--input chunk.jsonl] [--batch] [--run]    # run the winner + KEEP output
spendguard cache-audit | cache-test --script f.py [--run]   # prompt-caching: find + prove savings
spendguard cascade --ladder cheap,…,strong --intent X [--prompt …] --run           # cheap→verify→escalate
spendguard cache-stats | dedup --input f.jsonl --out u.jsonl | dedup-populate      # response cache + batch dedup

# work-done attribution (org → team × project), all sources
spendguard claude-code [show|sync|classify|work|story]   # mine ~/.claude → Claude Code spend + work (incremental, classified; alias: cc)
spendguard codex [show|sync|...]             # mine ~/.codex sessions → Codex est-value (channel=codex, billed=false)
spendguard chat [test|show|discover|classify|loop|work|story|sync|status|accept]   # claude.ai chat adapter (OPT-IN, on-device, macOS)
spendguard resources [show|snapshot|sync|discover]   # vast.ai GPU → org/team/project (discover [--agentic] recovers destroyed boxes)
#   more GPU clouds: RunPod / Modal / Lambda adapters ride `reconcile all` when their key is set (docs/PROVIDERS.md §GPU)
spendguard accounting                        # match actual provider USAGE → project via the conversations that ran each batch
spendguard signal                            # per project·intent·model efficiency signal (cost+quality+waste+reco) → server
spendguard workdone                          # work-done CONTEXT for spend (git + batch intents) → server (alias: work)
spendguard tag                               # re-assign a project tag (fix cwd-fallback mistags)

# cold start / corpus
spendguard bootstrap [--repo] [--transcripts]   # mine ALL history → corpus + insights (free, then estimate)
spendguard fetch-io [--cap 50]                  # recover real prompt+output from providers (free)
spendguard backfill [--intent-map …]            # seed corpus + graph from the batch ledgers (free)
spendguard mine-history {intents,graph,git} [--apply]   # reconstruct intents/edges from the repo (free; alias: history)
spendguard mine-conv {index,synth} [--run]      # mine session transcripts for the cost playbook (alias: conv)
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


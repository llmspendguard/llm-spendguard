"""spendguard CLI — one entry point for the whole toolkit.

  spendguard status | on | off          # kill-switch control
  spendguard report [--alert-threshold N]
  spendguard receipt [--json]             # running tally (today/7d/month); also auto-emitted per flow
  spendguard reconcile openai|anthropic [--since DATE] [--by-day]
  spendguard estimate --items N ...
  spendguard calibrate predict|show|pair|backtest   # LEARNED estimator: your history corrects the naive $
  spendguard audit [--ci]
  spendguard pricing                      # print the canonical table
"""
import sys

# The command surface, grouped the way you'd actually use it. The module docstring used to be the entire help —
# 10 of 60+ commands — printed for `--help`, `help`, `--version` AND every typo, always exiting 1.
# `_all_commands()` cross-checks this table against the real dispatch, so help can't silently drift from code.
_GROUPS = [
    ("start here", [
        ("scan", "what your local coding-agent work costs — no key, no network, 10s"),
        ("init", "set your caps + identity (deterministic; --quick for defaults)"),
        ("run", "`run -- <cmd>` gate ONE command; nothing written to your interpreter"),
        ("sources", "where can this machine spend? providers · agent tools · ungated venvs"),
        ("doctor", "is the gate enforcing HERE? keys, lanes, ledger status"),
        ("ask", "run ONE prompt across many LLMs — honest per-vendor coverage, $0 lanes"),
        ("comprehend", "fan a CORPUS across the $0 lanes (doc-mining/gap-analysis) — NOT Claude sub-agents; estimate-first"),
        ("serve", "the ask surface over localhost HTTP — POST /ask from any tool/language"),
        ("mcp", "spendguard's tools over MCP (stdio): model-advisor + spend/compaction queries"),
        ("install-mcp", "register `spendguard mcp` in Claude Code (~/.claude.json); --remove to undo"),
    ]),
    ("see the money", [
        ("receipt", "running tally: today / 7d / month, two axes"),
        ("report", "daily · weekly · monthly, per provider (+ leak alert)"),
        ("reconcile", "`reconcile all|openai|anthropic` — ledger vs the provider's BILL"),
        ("trust", "is what we recorded ≈ what you were billed?"),
        ("close", "monthly close: provider truth + residual, named"),
        ("focus-export", "the ledger as FinOps FOCUS 1.2 rows (json|csv) for any FinOps stack"),
        ("otel-ingest", "import an OTel GenAI trace (OpenLLMetry/Traceloop) → spend_events (idempotent)"),
        ("keys", "spend per API key (which workspace/project key)"),
        ("coverage", "which LLM-capable interpreters are NOT gated"),
    ]),
    ("spend less (measured, not guessed)", [
        ("advise", "cheapest config that HELD quality, per intent (the auto knob: reasoning='best-value')"),
        ("bakeoff", "measure cost×quality for a SLATE of untried models on an intent's tasks (feeds advise)"),
        ("effort-titrate", "learn the CHEAPEST reasoning effort that HOLDS quality, per (intent, model)"),
        ("calibrate", "learned estimator: your history corrects the naive $"),
        ("prompts", "lint the call corpus for prompt waste"),
        ("experiment", "A/B a cheaper config with graded output-equivalence"),
        ("maxtokens", "measured p99 bound for a call class (autotune's input)"),
        ("tokens", "per-provider token factors: `tokens calibrate` picks o200k→native AGENTICALLY from call_io stats (--dry-run=$0)"),
        ("realized", "what the changes actually saved"),
        ("savings", "what spendguard SAVED — measured + counterfactual, by source (the 3rd axis, never summed)"),
        ("measurement", "receipts for a judged number: `measurement inspect|list|reconstruct` — the judge/sample/rubric behind it"),
    ]),
    ("teams", [
        ("saas", "`saas link|sync|push|reconcile|reattribute` — org roll-up (opt-in)"),
        ("lanes", "subscription lanes: run meta prompts on your plan (+ `lanes set-model <lane> <model>`)"),
        ("tiers", "bulk-lane routing groups: show/validate + `tiers set <group> <model…>`"),
        ("truth", "push provider-truth totals (owner only)"),
    ]),
    ("setup & plumbing", [
        ("config", "`config` show all · `config set <section.key> <value>`"),
        ("install-hook", "gate EVERY process in a venv (opt-in; --uninstall removes)"),
        ("install-rule", "teach Claude/Cursor to route generated code through the gate"),
        ("install-skills", "add the /spend, /spendguard-* slash commands"),
        ("schedule", "OS-native daily sync (launchd / cron / schtasks)"),
        ("sync-prices", "refresh the price breadth layer now"),
        ("sync-catalog", "refresh the live model-catalog (validates model ids at dispatch)"),
        ("balances", "per-vendor metered prepay balance (sunk-pool vs on-demand), for routing"),
        ("reliability", "sweep every lane ($0) + metered provider (--run) for reachability; --remediate = agentic per-lane FIX (which login/quota/API to fix), cached; --notify = macOS notification on any red"),
        ("preflight", "resolve model ids (served + priced; stale→fix) BEFORE a batch — catches a bad id for $0"),
        ("verify", "self-check every money path: model ids · failover map · keys · economics (--probe = live)"),
        ("deploy", "run the full gate, then promote committed HEAD → green pointer (running MCP servers roll onto latest+best)"),
        ("release", "what THIS process serves vs the green pointer — are the MCP servers on the latest? (--json)"),
        ("pricing", "print the canonical price table"),
        ("audit", "fail CI if any code hardcodes a disagreeing price"),
        ("token-caps", "list every hardcoded output-token cap; --judge rules on the unjudged ones"),
        ("metadata", "model-metadata backbone health: LiteLLM cache + measured-cap drift"),
        ("estimate-divergence", "judge every recorded quote against the actual bill; fails on ungrounded ones"),
        ("estimate-literals", "every cost call fed literal token counts; --judge rules quote vs probe"),
        ("migrate", "rebuild spend_events from charges (exact Decimal); proves Σ preserved"),
    ]),
]


def _all_commands():
    return sorted({c for _g, items in _GROUPS for c, _d in items})


def help_text():
    out = ["spendguard — know what an LLM job will cost before you run it, and prove your ledger matches the bill.",
           "", "usage: spendguard <command> [args]    ·    spendguard --version", ""]
    for group, items in _GROUPS:
        out.append(f"{group}:")
        for cmd, desc in items:
            out.append(f"  {cmd:<15} {desc}")
        out.append("")
    out += ["Not listed here: the deeper surface (bootstrap, insights, compare, experiment internals, tag, …).",
            "Full reference: https://docs.llmspendguard.com/CLI/  ·  every setting: `spendguard config`"]
    return "\n".join(out)


def main(argv=None):
    """CLI entry. Wraps the dispatch so a MISSING PREREQUISITE (no provider key, no config) exits with one clean
    line instead of a raw traceback: `spendguard report` on a fresh install used to dump 14 lines ending in
    `KeyMissing`, while the reconcile branch caught the identical condition properly. A first run must never look
    like a crash — that is the whole first impression."""
    try:
        return _dispatch(argv)
    except RuntimeError as e:                             # KeyMissing subclasses RuntimeError
        print(f"spendguard: {e}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
    except BrokenPipeError:                               # `spendguard report | head` — not an error
        return 0


def _dispatch(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    cmd = argv[0] if argv else "status"
    rest = argv[1:]
    # ensure the gate is installed for this process so the advisor's OWN LLM calls (optimize/experiment/
    # reconstruct/mine/review/promote/cascade/brief --llm) are caged by caps.meta even when run via the CLI
    # outside a gated venv. Idempotent + fail-open.
    try:
        from . import gate
        gate.install()
    except Exception as _ge:                          # fail-open (the CLI must still run), but NOT silent: a failed
        import sys as _gs                             # self-install means this process's own meta LLM calls may be
        print(f"[spendguard] warning: gate.install() failed ({str(_ge)[:80]}); this process may be UNGATED — "   # ungated
              f"verify with `spendguard doctor`", file=_gs.stderr)
    if cmd in ("status", "doctor"):                   # first-run nudge when nothing's configured yet
        try:
            from . import config
            if not config.CONFIG_JSON.exists() and not config.saas_path().exists():
                print("ℹ not configured yet — run `spendguard init` (works standalone; optionally connect a team).\n")
        except Exception:
            pass
    if cmd in ("status", "on", "off", "doctor"):
        from . import gate
        return gate._cli(cmd, live="--live" in rest)   # doctor --live forces the full provider pull
    if cmd == "report":
        from . import report
        sys.argv = ["report"] + rest
        return report.main()
    if cmd in ("route-cost", "route_cost"):           # $0: TRUE-cost batch-vs-lane-vs-combo split for an intent at volume N
        from . import route_economics
        return route_economics.cmd(rest)
    if cmd == "focus-export":                         # ledger → FinOps FOCUS 1.2 rows (json|csv), read-only, $0
        from . import focus_export
        return focus_export.main(rest)
    if cmd in ("otel-ingest", "otel"):                # OTel GenAI trace → spend_events (import; idempotent; $0)
        from . import otel_ingest
        return otel_ingest.main(rest)
    if cmd == "preflight":                            # $0: resolve model ids (served + priced + stale→fix) BEFORE a batch
        from . import model_preflight
        return model_preflight.cmd(rest)
    if cmd == "verify":                               # forensic self-check: model ids · failover map · keys · economics (+--probe)
        from . import verify
        return verify.cmd(rest)
    if cmd == "reconcile":
        _PROV = {"openai", "anthropic", "all"}
        # A bare `spendguard reconcile` defaults to openai. But a FLAG in the first slot (e.g. `--since 2026-08-01`)
        # is NOT a provider — consuming it as `sub` silently ate the flag AND still ran openai; treat it as an
        # argument and keep the default. A non-flag word that isn't a known provider is a typo — error out rather
        # than silently run openai and print the wrong provider's numbers under the name the user typed.
        if rest and not rest[0].startswith("-"):
            sub, prov_args = rest[0], rest[1:]
            if sub not in _PROV:
                print(f"reconcile: unknown provider {sub!r} — choose one of {', '.join(sorted(_PROV))} "
                      f"(or omit it for openai)", file=sys.stderr)
                return 2
        else:
            sub, prov_args = "openai", rest
        if sub == "all":                                  # unified view: every spend source through the one loop
            from . import reconcile
            _since = None                                 # honor --since here too: it was parsed into prov_args but
            if "--since" in prov_args:                    # never passed on, so `reconcile all --since DATE` silently
                _i = prov_args.index("--since")           # reported the DEFAULT window instead of the requested one
                _since = prov_args[_i + 1] if _i + 1 < len(prov_args) else None
            reconcile.report(since=_since)
            return 0
        if sub == "anthropic":
            from . import reconcile_anthropic as r
        else:
            from . import reconcile_openai as r
        sys.argv = ["reconcile"] + prov_args
        try:
            return r.main()
        except RuntimeError as e:                          # e.g. a missing provider key — clean one-line exit, no traceback
            print(e)
            return 1
    if cmd == "estimate":
        # explicit submodule import: `from . import estimate` returns pricing.estimate (the function
        # __init__ re-exports), which shadows the submodule and broke this dispatch silently
        from .estimate import main as estimate_main
        sys.argv = ["estimate"] + rest
        return estimate_main()
    if cmd == "calibrate":                            # learned estimator over the captured corpus (zero spend)
        from . import calibrate
        return calibrate.main(rest)
    if cmd == "measurement":                          # receipts for a judged number — inspect/list/reconstruct ($0)
        from . import measurement
        return measurement.cmd(rest)
    if cmd == "audit":
        from . import audit as a
        sys.argv = ["audit"] + rest
        return a.main()
    if cmd == "token-caps":
        from . import token_caps
        return token_caps.cmd(rest)
    if cmd == "batch-submit":                         # GATED OpenAI Batch-API submit (estimate/submit) for a prepared .jsonl
        from . import openai_batch_cli
        return openai_batch_cli.submit_batch_jsonl(rest)
    if cmd == "batch-fetch":                          # poll a batch; download its output .jsonl when completed
        from . import openai_batch_cli
        return openai_batch_cli.fetch_batch_output(rest)
    if cmd == "ask":                                  # cross-LLM query surface — one prompt across models, honestly
        from . import crossllm
        return crossllm.cmd(rest)
    if cmd == "comprehend":                           # fan a CORPUS across the $0 lanes (doc-mining / gap-analysis)
        from . import comprehend as _comprehend       # instead of Claude-only sub-agents; estimate-first + gated
        return _comprehend.cmd(rest)
    if cmd == "serve":                                # the same cross-LLM ask surface over localhost HTTP
        from . import serve as _serve
        return _serve.cmd(rest)
    if cmd == "mcp":                                  # the model-advisor over MCP (stdio) — for any MCP client
        from . import mcp_server
        return mcp_server.cmd(rest)
    if cmd == "deploy":
        # Promote the current HEAD to the GREEN POINTER so already-running MCP servers roll onto the latest code —
        # but ONLY when it is COMMITTED and the full gate passes, so "green" (blue/green) == "green" (suite + ruff
        # + name gate). Running servers hand off at a safe point (between requests). --no-gate trusts a just-run
        # gate; --allow-dirty promotes an uncommitted tree deliberately (discouraged — the pointer then names code
        # no commit captures). This is the write path; `spendguard release` is the read-only status.
        import pathlib as _pl
        import subprocess as _sp
        from . import release as _rel
        r = list(rest)
        _no_gate, _allow_dirty = ("--no-gate" in r), ("--allow-dirty" in r)
        if _rel._tracked_dirty() and not _allow_dirty:
            print("deploy: the working tree has uncommitted changes to tracked files — commit first (the green "
                  "pointer must name committed code), or pass --allow-dirty deliberately.", file=sys.stderr)
            return 2
        gate_desc = "skipped (--no-gate)"
        if not _no_gate:
            suite = _pl.Path(__file__).resolve().parents[2] / "scripts" / "test" / "chunked_suite.py"
            print(f"deploy: running the full gate ({suite.name}) — a few minutes; the pointer only advances if it passes…")
            if _sp.run([sys.executable, str(suite)]).returncode != 0:
                print("deploy: 🔴 the gate FAILED — NOT promoting. Fix the suite, then re-run `spendguard deploy`.",
                      file=sys.stderr)
                return 1
            gate_desc = "chunked_suite green"
        rec = _rel.promote_release(gate=gate_desc, allow_dirty=_allow_dirty)
        print(f"deploy: 🟢 promoted {rec['short']} ({rec['describe']}) → green pointer  [{rec['gate']}]")
        print("        already-running MCP servers serve it on their next request (they hand off between requests).")
        return 0
    if cmd == "release":                              # read-only: what THIS process serves vs the green pointer
        import json as _json
        from . import release as _rel
        st = _rel.release_status()
        if "--json" in rest:
            print(_json.dumps(st, indent=2, default=str))
            return 0
        s, g = st.get("served") or {}, st.get("green") or {}
        print(f"served : {s.get('describe') or s.get('short') or '(unknown — not a git checkout)'}"
              + ("  [dirty: serving uncommitted edits]" if st.get("served_dirty") else ""))
        print(f"green  : {(g.get('describe') or g.get('short')) if g else '(none promoted — run `spendguard deploy`)'}")
        print(f"status : {'🔴 STALE' if st.get('stale') else ('🟢 up to date' if g else '⚪ no pointer yet')}")
        print(f"         {st.get('note')}")
        print(f"pointer: {st.get('pointer_path')}")
        return 0
    if cmd == "bakeoff":                              # measure cost×quality for a slate on a sample (fills untried models)
        from . import bakeoff
        return bakeoff.main(rest)
    if cmd == "effort-titrate":                       # learn the cheapest reasoning effort that holds quality per (intent,model)
        from . import effort_titration
        return effort_titration.main(rest)
    if cmd == "metadata":                             # model-metadata backbone health + measured-cap drift audit
        from . import metadata_audit
        return metadata_audit.main(rest)
    if cmd == "migrate":
        # THE clear, runnable migration: rebuild spend_events from charges under the exact-Decimal schema,
        # prove Σ is preserved to the last digit. Non-destructive (whole-file snapshot + old table renamed aside).
        from . import migrate_charges
        import json as _json
        stats = migrate_charges.run_cutover()
        if stats.get("already_cutover"):
            print("✓ already cut over — `charges` is retired and spend_events is the sole money-of-record. "
                  "Nothing to migrate; nothing was touched.")
            return 0
        keys = ("charges_rows", "migrated", "skipped_zero", "db_snapshot", "backup_table",
                "src_exact", "dst_exact", "residual", "reconciles")
        print(_json.dumps({k: stats.get(k) for k in keys}, indent=2))
        if not stats.get("reconciles"):
            print("REFUSED to trust: Σ charges != Σ spend_events. Investigate before removing charges.")
            return 1
        return 0
    if cmd == "estimate-literals":
        from . import estimate_literals
        return estimate_literals.cmd(rest)
    if cmd == "estimate-divergence":
        from . import estimate_divergence
        return estimate_divergence.cmd(rest)
    if cmd == "pricing":
        from . import pricing as p
        return p.main()
    if cmd == "providers":
        from . import pricing as p
        for prov, models in sorted(p.providers().items()):
            print(f"{prov} ({len(models)}): {', '.join(sorted(models))}")
        return 0
    if cmd == "coverage":                               # which LLM-calling venvs aren't gated (ungated realtime sources)
        from . import coverage
        return coverage.cmd(rest)
    if cmd == "gate-coverage":                          # per-INTERPRETER gate check across every python on the machine
        from . import setup
        return setup.cmd_coverage(rest)
    if cmd == "maxtokens":                              # data-driven max_tokens bound for a call-class sig
        from . import bulkgate
        if not rest:
            print("usage: spendguard maxtokens <sig> [current_max] [provider:model]   (sig from a TRUNCATED warning, "
                  "or bulkgate.sig(...); pass the model to get a reasoning-inclusive seed when the class is unmeasured)")
            return 2
        cur = next((int(a) for a in rest[1:] if str(a).isdigit()), None)
        _model = next((a for a in rest[1:] if ":" in str(a)), None)   # provider:model → reasoning-aware seed (#1)
        mt = bulkgate.maxtokens(rest[0], current_max=cur, model=_model)
        if not mt.get("n"):
            if mt.get("recommend"):        # unmeasured but a reasoning model → a reasoning-INCLUSIVE seed, not a guess
                print(f"no measured outputs for sig {rest[0]} yet — SEEDED (reasoning-inclusive): recommend "
                      f"max_tokens = {mt['recommend']}")
            else:
                print(f"no observed outputs for sig {rest[0]} yet (run a few calls first; truncations seen: "
                      f"{mt.get('truncations',0)}). Pass the provider:model to seed a reasoning-aware estimate.")
            if mt.get("warn"):
                print(f"  ⚠ {mt['warn']}")
            return 0
        print(f"sig {mt['sig']}  n={mt['n']}  truncations={mt['truncations']}")
        print(f"  output tokens: p50={mt['p50']}  p95={mt['p95']}  p99={mt['p99']}  max={mt['max']}")
        print(f"  → recommend max_tokens = {mt['recommend']}  (p99 × 1.5 — measured, not guessed)")
        if mt.get("warn"):
            print(f"  ⚠ {mt['warn']}")
        return 0
    if cmd == "dispatch":                               # live ADMISSION + QUEUE + reasoning-cut state — the parity view
        from . import dispatch                           # (same dispatch.admission_state() the MCP tool returns)
        st = dispatch.admission_state()
        if "--json" in rest:
            import json as _json
            print(_json.dumps(st, indent=2, default=str))
            return 0
        print("manage_all (universal admission): %s" % ("ON" if st.get("manage_all") else "OFF"))
        gov = st.get("governor") or {}
        print("governor — %d active key(s):" % len(gov))
        for k, b in gov.items():
            print("  %-26s limit=%s rpm=%s tpm=%s in_flight=%s waiting=%s"
                  % (k, b.get("limit"), b.get("rpm"), b.get("tpm"), b.get("in_flight"), b.get("waiting")))
        ll = st.get("learned_limits") or {}
        if ll:
            print("learned rate limits (self-calibrated from 429 + success headers):")
            for v, d in ll.items():
                print("  %-22s tpm=%s rpm=%s (%s)" % (v, d.get("tpm"), d.get("rpm"), d.get("source")))
        q = st.get("queue") or {}
        print("queue: pending=%s leased=%s parked=%s done=%s failed=%s" % (
            q.get("pending"), q.get("leased"), q.get("parked"), q.get("done"), q.get("failed")))
        dc = st.get("deadline_cancels") or {}
        if dc:
            print("deadline-cancel waste (reasoning cut mid-thought — invisible to the ledger, reconcile to see $):")
            for m, n in dc.items():
                print("  %-26s x%s" % (m, n))
        return 0
    if cmd == "config":
        from . import setup
        return setup.cmd_config(rest)
    if cmd == "init":
        from . import setup
        return setup.cmd_init(rest)
    if cmd in ("install-hook", "gate-venv"):          # gate every process in another venv (other repos)
        from . import setup
        return setup.cmd_install_hook(rest)
    if cmd == "schedule":                             # install the OS-native scheduler (launchd/cron/schtasks)
        from . import schedule
        return schedule.main(rest)
    if cmd == "install-skills":                       # deploy the / slash-commands into ~/.claude/skills
        from . import setup
        return setup.cmd_install_skills(rest)
    if cmd == "install-rule":                          # drop the spendguard usage rule into a CLAUDE.md
        from . import setup
        return setup.cmd_install_rule(rest)
    if cmd == "install-receipts":                      # surface the always-on tally in a host (claude-code|codex)
        from . import receipt
        return receipt.install_cli(rest)
    if cmd == "install-mcp":                            # register the MCP server in the client config (~/.claude.json)
        from . import mcp_server
        return mcp_server.register_client(remove="--remove" in rest)
    if cmd == "remote":                                # enforce the gate on remote/distributed compute (vast.ai)
        from . import remote
        return remote.cmd(rest)
    if cmd == "saas":                                  # team/org roll-up client seam (→ future server repo)
        from . import saas
        return saas.cmd(rest)
    if cmd == "resources":                             # non-LLM compute (vast.ai GPU) → same org/team/project model
        from . import resources
        return resources.cmd(rest)
    if cmd == "tag":                                   # re-assign a project tag (fix cwd-fallback mistags)
        from . import tag
        return tag.cmd(rest)
    if cmd == "calls":
        from . import calls
        return calls.cmd_summary(rest)
    if cmd == "receipt":                               # running tally (today/7d/month) → stdout; for the in-chat hook
        from . import receipt
        return receipt.cli(rest)
    if cmd == "backfill":
        from . import backfill
        return backfill.main(rest)
    if cmd in ("advise", "backtest"):   # backtest = advise --as-of <date>
        from . import advise
        return advise.main(rest)
    if cmd in ("optimize", "mine", "reconstruct"):   # Layer 2 — caged by caps.meta; estimate-only unless --run
        from . import advisor
        return advisor.main([cmd] + rest)
    if cmd in ("mine-history", "history"):           # deterministic post-event mining + graph enrichment (no spend)
        from . import history
        return history.main(rest)
    if cmd in ("mine-conv", "conv"):                  # conversation mining: index (no spend) + synth (caged)
        from . import conv
        return conv.main(rest)
    if cmd == "accounting":                            # match actual provider USAGE → project via conversations
        from . import conv
        return conv.attribute_cmd(rest)
    if cmd == "signal":                                # efficiency signal (cost+quality+waste+reco) → server
        from . import signal
        return signal.cmd(rest)
    if cmd in ("workdone", "work"):                    # work-done CONTEXT for spend (git + batch intents) → server
        from . import workdone
        return workdone.cmd(rest)
    if cmd in ("claude-code", "claudecode", "cc"):     # mine ~/.claude transcripts → CC spend + work (incremental)
        from . import claudecode
        return claudecode.main(rest)
    if cmd == "price":
        # Supply a VERIFIED price for a model spendguard cannot price. A --source is mandatory: spendguard
        # never invents a rate (an invented glm-5.2 stub once under-priced a model ~40%), so provenance is
        # the price of entry.
        from . import pricing as _pr
        r = list(rest)
        if not r or r[0].startswith("-"):
            print("usage: spendguard price <model> --in <$/1M> --out <$/1M> --source '<url or invoice>' "
                  "[--provider <name>] [--batch-in X] [--batch-out Y] [--cached-in Z]")
            return 2
        model = r[0]
        def _opt(flag, default=None):
            return r[r.index(flag) + 1] if flag in r and r.index(flag) + 1 < len(r) else default
        try:
            path, entry = _pr.set_price(
                model, _opt("--provider", "custom"), _opt("--in"), _opt("--out"), _opt("--source", ""),
                batch_in=_opt("--batch-in"), batch_out=_opt("--batch-out"), cached_in=_opt("--cached-in"))
        except ValueError as e:
            print(f"refused: {e}")
            return 2
        print(f"priced {model}: ${entry['in_']}/1M in · ${entry['out']}/1M out  (source: {entry['_source']})")
        print(f"  written to {path} — it now outranks the synced table, and past UNPRICED rows for this model")
        print("  can be re-costed with `spendguard reprice --model %s`." % model)
        return 0

    if cmd == "reprice":
        # Retroactively price ledger rows recorded 'unpriced' that a rate NOW resolves for (a model priced after the
        # fact, or a provider ambiguity since corrected). DRY-RUN by default; --apply commits through the ledger's
        # OWN audited update()/adjust() (never raw SQL), and snapshots the ledger first. --model scopes to one model.
        from . import budget as _bud, config as _cfg
        r = list(rest)

        def _ropt(flag, default=None):
            return r[r.index(flag) + 1] if flag in r and r.index(flag) + 1 < len(r) else default
        _model, _apply = _ropt("--model"), ("--apply" in r)
        if _apply:                                         # consistent snapshot BEFORE any write (integrity first)
            import sqlite3 as _sq
            import time as _t
            _db = _cfg.db_path()
            _bak = f"{_db}.bak_reprice_{int(_t.time())}"
            _src = _sq.connect(_db)
            _dst = _sq.connect(_bak)
            with _dst:
                _src.backup(_dst)
            _dst.close()
            _src.close()
            print(f"snapshot: {_bak}")
        plan = _bud.reprice_unpriced(model=_model, apply=_apply)
        from collections import Counter as _Counter
        methods = _Counter(p["method"] for p in plan)
        total = sum((p["cost"] or 0.0) for p in plan if p["method"] != "skip")
        scope = f" for {_model}" if _model else ""
        print(f"reprice{scope}: {len(plan)} unpriced row(s) — {methods.get('update', 0)} repriced in place, "
              f"{methods.get('adjust', 0)} adjusted (locked period), {methods.get('skip', 0)} left unpriced "
              f"(still unresolvable).")
        print(f"  ${total:.4f} of previously-unpriced usage {'FOLDED INTO' if _apply else 'would fold into'} the total"
              + ("." if _apply else " — pass --apply to commit (audited; a closed period gets a delta)."))
        return 0

    if cmd == "quarantine":
        # Repair for estimates already in the ledger that the plausibility rail now catches at record time.
        # Operator-driven ON PURPOSE: the request count behind an old batch row is not always recoverable, and
        # a repair that guessed it would repeat the bug it is repairing.
        from . import budget, pricing, config as _cfg
        rest_l = list(rest)
        _si = rest_l.index("--since") if "--since" in rest_l else -1   # `--since` as the LAST arg (no value) indexed
        since = rest_l[_si + 1] if 0 <= _si < len(rest_l) - 1 else _cfg.month_start_utc()   # past the end → IndexError
        if "--ts" in rest_l or "--row" in rest_l:
            # BOUNDS-CHECKED. `--row` as the FINAL argument indexed past the end and raised a bare
            # IndexError out of the CLI — a traceback where a usage message belongs, on the command that
            # EDITS THE LEDGER. _opt returns None for a flag given without a value, so the missing-target
            # check below can report it as the user error it is.
            def _opt(flag):
                i = rest_l.index(flag) if flag in rest_l else -1
                return rest_l[i + 1] if 0 <= i < len(rest_l) - 1 else None
            reason = _opt("--reason") or "impossible estimate"
            _row = _opt("--row")
            try:
                row = int(_row) if _row is not None else None
            except ValueError:
                print(f"quarantine: --row must be a rowid (got {_row!r})"); return 2
            ts = _opt("--ts")
            if row is None and ts is None:
                print("quarantine: --row <rowid> or --ts <timestamp> is required "
                      "(the flag was given without a value)"); return 2
            try:
                n = budget.quarantine_charge(ts=ts, reason=reason, row=row)
            except ValueError as e:                 # ambiguous timestamp — say so, never tag them all
                print(str(e))
                return 2
            target = f"row {row}" if row is not None else ts
            print(f"quarantined {n} row(s) at {target} — excluded from every total, kept for audit."
                  if n else f"no un-quarantined charge at {target}")
            return 0 if n else 1
        rows = budget.suspect_batches(since)
        if not rows:
            print(f"no batch charges since {since}")
            return 0
        print(f"batch charges since {since} — check the per-request arithmetic before quarantining:\n")
        print(f"  {'row':>6}  {'ts':<26}{'model':<20}{'cost':>10}  {'in_tok':>14}  {'ctx limit':>10}  caller")
        for r in rows:
            lim = pricing.max_input_tokens(r["model"])
            flag = " ←" if (lim and r["in_tok"] and r["in_tok"] > lim) else ""
            mark = " [quarantined]" if r["conv_id"] == budget.QUARANTINE_CONV else ""
            print(f"  {r['row']:>6}  {r['ts']:<26}{r['model']:<20}{r['cost']:>10,.2f}  {(r['in_tok'] or 0):>14,}  "
                  f"{(lim or 0):>10,}{flag}  {r['caller']}{mark}")
        print("\n  ← the batch's TOTAL input already exceeds one request's context window. Divide by the number"
              "\n    of requests it held: if that still exceeds the limit, the estimate was impossible."
              "\n    Quarantine it with:  spendguard quarantine --row <row> --reason '<why>'"
              "\n    (--row, not --ts: a timestamp can cover several charges, and tagging them all would be"
              "\n     a worse bug than the one being repaired.)")
        return 0

    if cmd == "codex":                                 # mine ~/.codex sessions → Codex est-value (channel=codex)
        from . import codex
        return codex.main(rest)
    if cmd == "chat":                                  # OPT-IN claude.ai chat adapter (session API, on-device, macOS)
        from . import chat
        return chat.main(rest)
    if cmd in ("lanevalue", "lane-value"):             # price subscription lanes with NO session miner (gemini/zai)
        from . import lane_value                        # → est-value, from the calls ledger (mirrors `codex`)
        return lane_value.main(rest)
    if cmd in ("fetch-io", "fetchio"):                # recover real prompt+output samples from providers (free)
        from . import callio
        return callio.main(rest)
    if cmd in ("callio-status", "corpus-status"):     # how full is the replay corpus per intent (before a sweep) — $0
        from . import callio
        return callio.status_main(rest)
    if cmd in ("codex-gc", "codex-prune"):            # prune codex CLI's shell-snapshot residue (dry-run default) — $0
        from . import codex_exec
        import argparse as _agc
        _p = _agc.ArgumentParser(prog="spendguard codex-gc")
        _p.add_argument("--max-age-days", type=float, default=7.0, help="prune shell snapshots older than this (default 7)")
        _p.add_argument("--apply", action="store_true", help="actually delete (default: dry-run report)")
        _a = _p.parse_args(rest)
        r = codex_exec.gc_shell_snapshots(max_age_days=_a.max_age_days, apply=_a.apply)
        _mb = r["bytes"] / 1e6
        print(f"codex shell-snapshot gc — {r['dir']}")
        if r["error"]:
            print(f"  🔴 could not scan: {r['error']}")
            return 2
        print(f"  examined {r['examined']}, stale (>{r['cutoff_days']:g}d) {r['stale']} = {_mb:.1f} MB"
              + (f", skipped {r['skipped']}" if r["skipped"] else ""))
        if _a.apply:
            print(f"  DELETED {r['deleted']} file(s), freed ~{_mb:.1f} MB"
                  + (f" ({r['skipped']} could not be removed — see perms)" if r["skipped"] else ""))
        else:
            print(f"  DRY-RUN — re-run with --apply to delete the {r['stale']} stale file(s) (~{_mb:.1f} MB). "
                  f"Recent snapshots are never touched.")
            print("  Opt-in periodic cleanup: schedule `spendguard codex-gc --apply` (launchd/cron or `spendguard schedule`).")
        return 0
    if cmd == "review":                               # practice audit (smart-vs-wasteful) — caged, estimate-first
        from . import review
        return review.main(rest)
    if cmd in ("cache-audit", "cacheaudit"):          # find prompt-caching savings (no spend)
        from . import cacheaudit
        return cacheaudit.main(rest)
    if cmd in ("cache-test", "cachetest"):            # empirically prove caching engages (caged, estimate-first)
        from . import cachetest
        return cachetest.main(rest)
    if cmd == "brief":                                # "this is what we need to do" → confirm-or-correct plan
        from . import brief
        return brief.main(rest)
    if cmd == "experiment":                           # A/B efficiency lab: cost↓ + same-output (caged, estimate-first)
        from . import experiment
        return experiment.main(rest)
    if cmd == "models":                               # per-model learnings/profiles (auto-applied on every call)
        from . import models
        return models.cmd(rest)
    if cmd == "promote":                              # run a winning config on a chunk + KEEP output (workload)
        from . import experiment
        return experiment.promote_main(rest)
    if cmd in ("cache-stats", "semcache"):            # semantic response cache stats (opt-in cost saver)
        from . import semcache
        return semcache.cmd(rest)
    if cmd == "dedup":                                # collapse a batch jsonl (within-batch + already-cached)
        from . import semcache
        return semcache.dedup_main(rest)
    if cmd == "dedup-populate":                       # seed the cache from completed results → free re-runs
        from . import semcache
        return semcache.populate_main(rest)
    if cmd == "cascade":                              # cost-aware routing: cheap→verify→escalate (workload)
        from . import cascade
        return cascade.cmd(rest)
    if cmd in ("reconcile-ledger", "ledger-sync", "leaks"):   # local ledger vs provider billing → find leaks
        from . import ledger_sync
        return ledger_sync.main(rest)
    if cmd in ("trust", "trust-check"):               # provider billing vs recorded — the daily double-count guard
        from . import trust
        return trust.cmd(rest)
    if cmd in ("cross-check", "crosscheck"):          # free price drift check vs OpenRouter's public JSON
        from . import pricing as p
        try:
            rows, matched, total = p.cross_check_openrouter()
        except Exception as e:
            print(f"cross-check failed (network?): {e}"); return 1
        print(f"price cross-check vs OpenRouter — {matched}/{total} models matched "
              f"(frontier models not on OpenRouter don't match; that's coverage, not error)")
        print(f"  {'model':<24}{'our in/out':>16}{'OR in/out':>16}  flag")
        for model, oi, ri, oo, ro, flag in rows:
            print(f"  {model[:23]:<24}{('$%.2f/$%.2f' % (oi, oo)):>16}{('$%.2f/$%.2f' % (ri, ro)):>16}  "
                  f"{'⚠️ ' + flag if flag == 'DRIFT' else flag}")
        if not rows:
            print("  (no overlapping models — your table is mostly frontier models OpenRouter doesn't list.)")
        return 0
    if cmd == "bootstrap":                            # cold-start: mine all history → corpus + insights
        from . import bootstrap
        return bootstrap.main(rest)
    if cmd == "validate":                             # living insights — re-check learnings vs current corpus
        from . import validate
        return validate.main(rest)
    if cmd == "realized":                             # measured before/after $ per call around insight adoptions
        from . import realized
        return realized.main()
    if cmd == "savings":                              # the 3rd axis: what spendguard SAVED (measured + counterfactual)
        from . import guard as _g
        s, ds = _g.saved_since(), _g.decisions_summary()
        if "--json" in rest:
            import json as _j
            print(_j.dumps({"saved": s, "decisions": ds}, indent=2, default=str))
            return 0
        print("spendguard SAVINGS — a THIRD axis, kept SEPARATE from real-$ and est-value (never summed into either):")
        print(f"  certain (measured)     ${s.get('certain', 0):.2f}")
        print(f"  counterfactual (est)   ${s.get('counterfactual', 0):.2f}")
        print(f"  ── total guarded       ${s.get('total', 0):.2f}")
        _by = s.get("by_source") or {}
        if _by:
            print("  by source: " + "   ".join(f"{k} ${v:.2f}" for k, v in sorted(_by.items(), key=lambda kv: -kv[1])))
        print(f"  decisions booked: {ds.get('decisions', 0)}  (best-value / advisor swaps, each priced vs its counterfactual)")
        for row in (ds.get("by_intent") or [])[:8]:
            print(f"    {str(row.get('intent', '?'))[:44]:44} {row.get('decisions', 0):>5} dec  ${row.get('saved_usd', 0):.2f}")
        return 0
    if cmd == "tokens":                               # per-provider token factors: `tokens show` / `tokens calibrate` ($0)
        from . import provider_tokens
        return provider_tokens.cmd(rest)
    if cmd == "prompts":                              # prompt-efficiency lint over the call corpus (zero spend)
        from . import prompts
        return prompts.main()
    if cmd == "close":                                # monthly close, client view (provider-truth totals + leak line)
        from . import close
        return close.main()
    if cmd == "truth":                                # per-day provider-truth totals; --push syncs (keys stay local)
        from . import truth
        return truth.main()
    if cmd == "sources":                              # where CAN this machine spend: providers · agent tools ·
        from . import sources                         # interpreters. One discovery, free, never reads your code.
        return sources.main(rest)
    if cmd == "scan":                                 # THE FIRST RUN: local transcripts only — no key, no network,
        from . import scan                            # no LLM, no writes outside SPENDGUARD_HOME. Safe via uvx.
        return scan.main(rest)
    if cmd == "run":                                  # gate ONE command via the child's PYTHONPATH (no site-packages
        from . import runner                          # write, nothing persists) — the DEFAULT way to gate since 0.8
        return runner.main(rest)
    if cmd == "lanes":                                # subscription-lane activation status (+ --probe live check)
        from . import lanes
        return lanes.main(rest)
    if cmd == "tiers":                                # bulk-lane routing GROUPS: show/validate + `tiers set …`
        from . import tier_config
        return tier_config.main(rest)
    if cmd == "register-critical":                    # a CONSUMER (e.g. warden) pins its no-substitution vendor-critical intents
        import argparse
        from . import lane_balance
        ap = argparse.ArgumentParser(prog="spendguard register-critical")
        ap.add_argument("patterns", nargs="*", help="intent patterns to pin (no-substitution), e.g. warden:card_faithful*")
        ap.add_argument("--source", help="who is registering these (e.g. warden) — recorded so doctor attributes them")
        ap.add_argument("--list", action="store_true", help="show the current pins + who registered each")
        a = ap.parse_args(rest)
        cov = (lane_balance.bandit_list_coverage() or {}).get("bandit_denylist", {})
        if a.list or not a.patterns:
            src = cov.get("sources") or {}
            print("advisor.bandit_denylist — no-substitution pins (a cross-vendor panel is never collapsed to one vendor):")
            for e in (cov.get("entries") or []):
                print(f"  {e:<42} {('← ' + src[e]) if e in src else ''}")
            if not cov.get("entries"):
                print("  (none) — register with: spendguard register-critical <intent-pattern…> --source <name>")
            return 0
        merged = lane_balance.register_critical(a.patterns, source=a.source)
        print(f"registered {len(a.patterns)} pin(s){(' from ' + a.source) if a.source else ''} → "
              f"advisor.bandit_denylist ({len(merged)} total, deduped)")
        return 0
    if cmd == "keys":                                 # per-KEY spend (which workspace/project key) — local-only
        from . import budget, config as _c
        since = None
        for i, a in enumerate(rest):
            if a == "--since" and i + 1 < len(rest):
                since = rest[i + 1]
        # `_dt` is the stdlib datetime module imported two lines up; `_dt.config` does not exist, so this
        # raised AttributeError every time --since was omitted — the DEFAULT path of the command.
        since = since or _c.month_start_utc()
        prof = _c._key_profile()
        print(f"per-key workload spend since {since}" + (f"  (active key profile: {prof})" if prof else ""))
        rows = sorted(budget.by_key(since=since).items(), key=lambda x: -x[1]["cost"])
        if not rows:
            print("  (no workload charges in the window)")
        for (prov, fp), v in rows:
            note = "  ← rows before key stamping / no key env resolved" if fp == "(none)" else ""
            print(f"  {prov:<11}{fp:<16}${v['cost']:>10.2f}  {v['calls']:>6} calls{note}")
        print("  (fingerprint = sha256[:8]:last4 of the serving key — local-only, never pushed)")
        return 0
    if cmd == "insights":                             # list / export(scrubbed) / import community learnings
        from . import share
        return share.main(rest)
    if cmd == "compare":
        from . import compare
        return compare.main(rest)
    if cmd in ("sync-prices", "sync"):
        from . import sync
        return sync.main(rest)
    if cmd == "sync-catalog":
        from . import catalog
        return catalog.main(rest)
    if cmd == "balances":
        from . import balances
        return balances.main(rest)
    if cmd == "reliability":
        from . import reliability
        return reliability.main(rest)
    if cmd in ("refresh-prices", "refresh"):
        from . import refresh
        return refresh.main(rest)
    if cmd in ("check-prices", "freshness"):
        from . import pricing as p
        v, days, stale = p.freshness()
        flag = f"  ⚠️ STALE (>{p.STALE_AFTER_DAYS}d) — re-verify against the source below" if stale else "  (fresh)"
        print(f"prices verified {v} ({days} days ago){flag}")
        print(f"  source : {p.PRICING_SOURCE}")
        print("  config : edit prices.json in the package, or ~/.spendguard/prices.json (or SPENDGUARD_PRICES)")
        for prov, models in sorted(p.providers().items()):
            print(f"  {prov}: {len(models)} models")
        return 2 if stale else 0
    # An explicit help request EXITS 0 and prints the real, grouped surface. Before this, `--help`, `-h`, `help`,
    # `--version` and a typo all printed the same 9-line module docstring — 10 of 60+ commands — and exited 1.
    if cmd in ("--help", "-h", "help", "--commands"):
        print(help_text())
        return 0
    if cmd in ("--version", "-V", "version"):
        from . import __version__, release as _rel
        _s = _rel.served_sha()
        _tag = (f" ({_s['describe']})" + ("  [dirty]" if _s.get("dirty") else "")) if _s else ""
        print(f"llm-spendguard {__version__}{_tag}")
        if _rel.stale_vs_green():                      # this process is behind the deployed green commit
            _g = _rel.green_pointer() or {}
            print(f"  ⚠ STALE vs green pointer {_g.get('short')} — `spendguard deploy` promotes; "
                  "MCP servers roll onto it on their next request. `spendguard release` for detail.")
        return 0
    import difflib
    near = difflib.get_close_matches(cmd, _all_commands(), n=3, cutoff=0.55)
    print(f"unknown command {cmd!r}" + (f" — did you mean: {', '.join(near)}?" if near else ""), file=sys.stderr)
    print("`spendguard --help` lists every command.", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())

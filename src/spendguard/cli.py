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
    ]),
    ("see the money", [
        ("receipt", "running tally: today / 7d / month, two axes"),
        ("report", "daily · weekly · monthly, per provider (+ leak alert)"),
        ("reconcile", "`reconcile all|openai|anthropic` — ledger vs the provider's BILL"),
        ("trust", "is what we recorded ≈ what you were billed?"),
        ("close", "monthly close: provider truth + residual, named"),
        ("keys", "spend per API key (which workspace/project key)"),
        ("coverage", "which LLM-capable interpreters are NOT gated"),
    ]),
    ("spend less (measured, not guessed)", [
        ("advise", "cheapest config that HELD quality, per intent"),
        ("calibrate", "learned estimator: your history corrects the naive $"),
        ("prompts", "lint the call corpus for prompt waste"),
        ("experiment", "A/B a cheaper config with graded output-equivalence"),
        ("maxtokens", "measured p99 bound for a call class (autotune's input)"),
        ("realized", "what the changes actually saved"),
    ]),
    ("teams", [
        ("saas", "`saas link|sync|push|reconcile|reattribute` — org roll-up (opt-in)"),
        ("lanes", "subscription lanes: run meta prompts on your plan"),
        ("truth", "push provider-truth totals (owner only)"),
    ]),
    ("setup & plumbing", [
        ("config", "`config` show all · `config set <section.key> <value>`"),
        ("install-hook", "gate EVERY process in a venv (opt-in; --uninstall removes)"),
        ("install-rule", "teach Claude/Cursor to route generated code through the gate"),
        ("install-skills", "add the /spend, /spendguard-* slash commands"),
        ("schedule", "OS-native daily sync (launchd / cron / schtasks)"),
        ("sync-prices", "refresh the price breadth layer now"),
        ("pricing", "print the canonical price table"),
        ("audit", "fail CI if any code hardcodes a disagreeing price"),
        ("token-caps", "list every hardcoded output-token cap; --judge rules on the unjudged ones"),
        ("estimate-divergence", "judge every recorded quote against the actual bill; fails on ungrounded ones"),
        ("estimate-literals", "every cost call fed literal token counts; --judge rules quote vs probe"),
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
    except Exception:
        pass
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
    if cmd == "reconcile":
        sub = rest[0] if rest else "openai"
        if sub == "all":                                  # unified view: every spend source through the one loop
            from . import reconcile
            reconcile.report()
            return 0
        if sub == "anthropic":
            from . import reconcile_anthropic as r
        else:
            from . import reconcile_openai as r
        sys.argv = ["reconcile"] + rest[1:]
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
    if cmd == "audit":
        from . import audit as a
        sys.argv = ["audit"] + rest
        return a.main()
    if cmd == "token-caps":
        from . import token_caps
        return token_caps.cmd(rest)
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
            print("usage: spendguard maxtokens <sig> [current_max]   (sig from a TRUNCATED warning, or bulkgate.sig(...))")
            return 2
        cur = int(rest[1]) if len(rest) > 1 and str(rest[1]).isdigit() else None
        mt = bulkgate.maxtokens(rest[0], current_max=cur)
        if not mt.get("n"):
            print(f"no observed outputs for sig {rest[0]} yet (run a few calls first; truncations seen: {mt.get('truncations',0)})")
            return 0
        print(f"sig {mt['sig']}  n={mt['n']}  truncations={mt['truncations']}")
        print(f"  output tokens: p50={mt['p50']}  p95={mt['p95']}  p99={mt['p99']}  max={mt['max']}")
        print(f"  → recommend max_tokens = {mt['recommend']}  (p99 × 1.5 — measured, not guessed)")
        if mt.get("warn"):
            print(f"  ⚠ {mt['warn']}")
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
        print("  can be re-costed with `spendguard reconcile all`.")
        return 0

    if cmd == "quarantine":
        # Repair for estimates already in the ledger that the plausibility rail now catches at record time.
        # Operator-driven ON PURPOSE: the request count behind an old batch row is not always recoverable, and
        # a repair that guessed it would repeat the bug it is repairing.
        from . import budget, pricing, config as _cfg
        rest_l = list(rest)
        since = rest_l[rest_l.index("--since") + 1] if "--since" in rest_l else _cfg.month_start_utc()
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
    if cmd in ("fetch-io", "fetchio"):                # recover real prompt+output samples from providers (free)
        from . import callio
        return callio.main(rest)
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
    if cmd == "keys":                                 # per-KEY spend (which workspace/project key) — local-only
        import datetime as _dt
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
        from . import __version__
        print(f"llm-spendguard {__version__}")
        return 0
    import difflib
    near = difflib.get_close_matches(cmd, _all_commands(), n=3, cutoff=0.55)
    print(f"unknown command {cmd!r}" + (f" — did you mean: {', '.join(near)}?" if near else ""), file=sys.stderr)
    print("`spendguard --help` lists every command.", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())

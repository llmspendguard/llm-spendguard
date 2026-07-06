"""spendguard CLI — one entry point for the whole toolkit.

  spendguard status | on | off          # kill-switch control
  spendguard report [--alert-threshold N]
  spendguard receipt [--json]             # running tally (today/7d/month); also auto-emitted per flow
  spendguard reconcile openai|anthropic [--since DATE] [--by-day]
  spendguard estimate --items N ...
  spendguard audit [--ci]
  spendguard pricing                      # print the canonical table
"""
import sys


def main(argv=None):
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
        return gate._cli(cmd)
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
        from . import estimate as e
        sys.argv = ["estimate"] + rest
        return e.main()
    if cmd == "audit":
        from . import audit as a
        sys.argv = ["audit"] + rest
        return a.main()
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
    if cmd == "prompts":                              # prompt-efficiency lint over the call corpus (zero spend)
        from . import prompts
        return prompts.main()
    if cmd == "close":                                # monthly close, client view (provider-truth totals + leak line)
        from . import close
        return close.main()
    if cmd == "truth":                                # per-day provider-truth totals; --push syncs (keys stay local)
        from . import truth
        return truth.main()
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
    print(__doc__)
    return 1


if __name__ == "__main__":
    sys.exit(main())

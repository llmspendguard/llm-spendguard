"""spendguard CLI — one entry point for the whole toolkit.

  spendguard status | on | off          # kill-switch control
  spendguard report [--alert-threshold N]
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
    if cmd in ("status", "on", "off", "doctor"):
        from . import gate
        return gate._cli(cmd)
    if cmd == "report":
        from . import report
        sys.argv = ["report"] + rest
        return report.main()
    if cmd == "reconcile":
        sub = rest[0] if rest else "openai"
        if sub == "anthropic":
            from . import reconcile_anthropic as r
        else:
            from . import reconcile_openai as r
        sys.argv = ["reconcile"] + rest[1:]
        return r.main()
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
    if cmd == "config":
        from . import setup
        return setup.cmd_config(rest)
    if cmd == "init":
        from . import setup
        return setup.cmd_init(rest)
    if cmd in ("install-hook", "gate-venv"):          # gate every process in another venv (other repos)
        from . import setup
        return setup.cmd_install_hook(rest)
    if cmd == "install-skills":                       # deploy the / slash-commands into ~/.claude/skills
        from . import setup
        return setup.cmd_install_skills(rest)
    if cmd == "install-rule":                          # drop the spendguard usage rule into a CLAUDE.md
        from . import setup
        return setup.cmd_install_rule(rest)
    if cmd == "coverage":                              # which interpreters/venvs are actually gated? (multi-version)
        from . import setup
        return setup.cmd_coverage(rest)
    if cmd == "saas":                                  # team/org roll-up client seam (→ future server repo)
        from . import saas
        return saas.cmd(rest)
    if cmd == "resources":                             # non-LLM compute (vast.ai GPU) → same org/team/project model
        from . import resources
        return resources.cmd(rest)
    if cmd == "calls":
        from . import calls
        return calls.cmd_summary(rest)
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
        print(f"  config : edit prices.json in the package, or ~/.spendguard/prices.json (or SPENDGUARD_PRICES)")
        for prov, models in sorted(p.providers().items()):
            print(f"  {prov}: {len(models)} models")
        return 2 if stale else 0
    print(__doc__)
    return 1


if __name__ == "__main__":
    sys.exit(main())

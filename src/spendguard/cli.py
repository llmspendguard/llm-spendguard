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
    if cmd in ("status", "on", "off"):
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

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

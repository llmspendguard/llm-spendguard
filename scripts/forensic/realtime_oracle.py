"""DEV-ONLY realtime oracle PRINTER — a thin wrapper over `spendguard.realtime_oracle` (the package home for the
logic). The reconcile (`ledger_sync.reconcile_realtime` under SPENDGUARD_ADMIN_ORACLE) is what RECORDS this into the
ledger; this script just shows the timing-matched breakdown for inspection. Needs OPENAI_ADMIN_KEY / ANTHROPIC_ADMIN_KEY.

  .venv.nosync/bin/python scripts/forensic/realtime_oracle.py [SINCE=YYYY-MM-01]
"""
import sys
from collections import defaultdict
from spendguard import realtime_oracle, attribution


def main(argv=None):
    argv = argv or sys.argv[1:]
    since = argv[0] if argv else "2026-06-01"
    rows, meta = realtime_oracle.by_project_day(since)
    pt = attribution.project_team_map(attribution.taxonomy()[0])
    by_proj, by_org = defaultdict(float), defaultdict(float)
    for r in rows:
        by_proj[r["project"]] += r["cost"]
        by_org[pt.get(r["project"], ("(unmapped)", ""))[0]] += r["cost"]
    print(f"=== REALTIME timing-matched oracle (since {since}) ===")
    print(f"  org-wide CEILING: ${meta['ceiling']:.2f}   OURS: ${meta['ours_total']:.2f}   OTHER-org: ${meta['other_org']:.2f}")
    print("  OURS by ORG (project→org via the shared taxonomy):")
    for org, v in sorted(by_org.items(), key=lambda x: -x[1]):
        print(f"    {org:16} ${v:.2f}")
    print("  OURS by PROJECT:")
    for p, v in sorted(by_proj.items(), key=lambda x: -x[1]):
        print(f"    {p:16} ${v:.2f}")
    print("  (RECORDED into the ledger by `reconcile_realtime` under SPENDGUARD_ADMIN_ORACLE — this is just the view.)")
    return 0


if __name__ == "__main__":
    sys.exit(main())

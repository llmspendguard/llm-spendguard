"""Monthly CLOSE (client view) — the local half of the close statement.

Prints the month's PROVIDER-TRUTH totals (computed locally from the providers' own usage/cost APIs —
same rows `spendguard truth --push` syncs), and for the CURRENT month adds the ledger leak line
(accounted-vs-provider, the completeness verdict). The full attributed statement — classes, projects,
teams, per-provider residual — lives on the org server (`/statements`, CSV export), which reconciles
its ledger against the truth rows this machine pushes. `--csv` writes the local view for records.

CLI: `spendguard close [--month YYYY-MM] [--csv PATH]`. Zero LLM spend.
"""
import datetime


def month_window(month):
    """(start, end) ISO dates for YYYY-MM."""
    y, m = int(month[:4]), int(month[5:7])
    start = f"{month}-01"
    end = f"{y + 1}-01-01" if m == 12 else f"{y}-{m + 1:02d}-01"
    return start, end


def build(month):
    """{month, providers: [{provider, usd, days}], total_usd, current_month, forecast?} from local
    provider truth. For the OPEN month with ≥5 observed days, forecast = MTD + remaining calendar days ×
    the observed daily median (p50) / 90th-percentile (p90) — a RUN-RATE projection, labeled as such
    (it extrapolates the month's own daily distribution; it does not model planned jobs)."""
    from . import truth
    start, end = month_window(month)
    per = {}
    days = {}
    by_day = {}
    for r in truth.rows(since=start):
        if start <= r["day"] < end:
            per[r["provider"]] = per.get(r["provider"], 0.0) + r["usd"]
            days.setdefault(r["provider"], set()).add(r["day"])
            by_day[r["day"]] = by_day.get(r["day"], 0.0) + r["usd"]
    providers = [{"provider": p, "usd": round(v, 6), "days": len(days[p])}
                 for p, v in sorted(per.items(), key=lambda kv: -kv[1])]
    today = datetime.date.today()
    out = {"month": month, "providers": providers,
           "total_usd": round(sum(p["usd"] for p in providers), 6),
           "current_month": month == today.strftime("%Y-%m")}
    if out["current_month"] and len(by_day) >= 5:
        daily = sorted(by_day.values())
        p50 = daily[len(daily) // 2]
        p90 = daily[min(len(daily) - 1, int(0.9 * (len(daily) - 1) + 0.999))]
        last_dom = (datetime.date(today.year + (today.month == 12), today.month % 12 + 1, 1)
                    - datetime.timedelta(days=1)).day
        remaining = max(0, last_dom - today.day)
        out["forecast"] = {"days_observed": len(by_day), "remaining_days": remaining,
                           "p50_usd": round(out["total_usd"] + remaining * p50, 2),
                           "p90_usd": round(out["total_usd"] + remaining * p90, 2)}
    return out


def to_csv(stmt):
    lines = [f"# Monthly close (client provider-truth view),{stmt['month']}",
             f"total_usd,{stmt['total_usd']:.2f}", "", "provider,usd,days"]
    lines += [f"{p['provider']},{p['usd']:.2f},{p['days']}" for p in stmt["providers"]]
    return "\n".join(lines) + "\n"


def main(argv=None):
    import sys, argparse
    ap = argparse.ArgumentParser(prog="spendguard close",
                                 description="monthly close, client view: provider-truth totals (+ leak line for the current month)")
    ap.add_argument("--month", default=datetime.date.today().strftime("%Y-%m"), help="YYYY-MM (default: current)")
    ap.add_argument("--csv", help="also write the close to this CSV path")
    ap.add_argument("--account", action="store_true",
                    help="ACCOUNT-level view: provider truth is account-wide; on a shared account each org's "
                         "statement residual includes its siblings — this view shows the account axis whole")
    a = ap.parse_args(sys.argv[2:] if argv is None else argv)
    if not (len(a.month) == 7 and a.month[4] == "-"):
        print("close: --month must be YYYY-MM"); return 2
    s = build(a.month)
    print(f"monthly close — {s['month']}   provider truth Σ ${s['total_usd']:,.2f}")
    for p in s["providers"]:
        print(f"  {p['provider']:<12} ${p['usd']:>10,.2f}   ({p['days']} billed days)")
    if not s["providers"]:
        print("  (no provider spend found for this month)")
    if s["current_month"]:
        f = s.get("forecast")
        if f:
            print(f"  run-rate forecast: month-end ~${f['p50_usd']:,.2f} (p50) … ${f['p90_usd']:,.2f} (p90) "
                  f"— {f['days_observed']} observed days × {f['remaining_days']} remaining (extrapolation, not a plan)")
        try:                                        # accounted-vs-provider completeness for the open month
            from . import ledger_sync
            line = ledger_sync.leak_line(month_window(a.month)[0])
            if line:
                print("  " + line)
        except Exception:
            print("  ⚠ leak check could not run — accounted-vs-truth UNKNOWN for the open month")
    if a.account:
        print("  ── account axis (shared provider account) ──")
        print("  truth above is ACCOUNT-wide: on a shared account, each org's /statements residual includes")
        print("  its sibling orgs' spend. Machine-accounted vs provider for the open month:")
        try:
            from . import ledger_sync
            line = ledger_sync.leak_line(month_window(a.month)[0])
            print("  " + (line or "leak line unavailable"))
        except Exception:
            print("  ⚠ accounted-vs-provider check could not run — UNKNOWN, not zero")
        print("  per-org residuals: each org's /statements (owner org carries the account truth)")
    print("  full attributed statement (classes/projects/teams + named residual): your org server → /statements")
    if a.csv:
        with open(a.csv, "w") as f:
            f.write(to_csv(s))
        print(f"  wrote {a.csv}")
    return 0

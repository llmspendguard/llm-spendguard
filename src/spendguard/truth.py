"""Provider-TRUTH sync — push per-day provider totals to the org server, keys never leave this machine.

The design decision behind this module: API-based reconciliation beats invoice CSV import, but the
provider/admin keys stay CLIENT-side (the standing hard rule: privileged keys are never part of a hosted
main path). The org admin's machine — which already holds the keys and already pulls these exact APIs for
the daily report — computes the truth and pushes only the RESULTING day totals: {day, provider, usd}.
The server's monthly close statement then shows variance vs truth without ever holding a key.
(Server-side custody of read-only usage-scoped keys = a later Enterprise option, not this.)

Reuses the report's fetchers verbatim (openai_by_day / anth.cost_by_day / gpu_by_day) — no new provider
code, and truth here is definitionally the same numbers the report prints. Zero LLM spend (usage/cost
APIs are free). CLI: `spendguard truth [--push]` — preview first, push is explicit; the push follows the
tolerant contract (a server without /v1/truth yet → a friendly skip, never an error)."""
import datetime


def rows(since=None):
    """[{day, provider, usd}] for every day ≥ `since` (default: 35 days — covers a full statement month
    plus reconciliation lag) across all sources the report knows: openai · anthropic · vastai (GPU)."""
    from . import report
    from . import reconcile_anthropic as anth
    if since is None:
        since = (datetime.date.today() - datetime.timedelta(days=35)).strftime("%Y-%m-%d")
    out = []
    oai, _pending = report.openai_by_day()
    an, _models = anth.cost_by_day(since=since)
    gpu, _gpu_err = report.gpu_by_day(since)   # (by_day, error) — error → empty dict, vastai just absent this run
    for provider, by_day in (("openai", oai), ("anthropic", an), ("vastai", gpu)):
        for day, usd in sorted(by_day.items()):
            if day >= since and float(usd) > 0:
                out.append({"day": day, "provider": provider, "usd": round(float(usd), 6)})
    return out


def push(since=None, dry=False):
    """Push truth rows → POST /v1/truth. Honors visibility (private = nothing leaves); tolerates a server
    that doesn't implement the endpoint yet (the same forward-compat pattern as push_insights)."""
    from . import saas
    c = saas.conn()
    if c.get("visibility", "private") == "private":
        return {"skipped": "visibility=private — nothing leaves this machine"}
    payload = {"truth": rows(since)}
    if dry:
        return payload
    try:
        return saas._request("POST", "/v1/truth", payload)
    except RuntimeError as e:
        if " 404" in str(e) or " 405" in str(e):
            return {"skipped": "server has no /v1/truth endpoint yet"}
        raise


def main(argv=None):
    import sys, argparse
    ap = argparse.ArgumentParser(prog="spendguard truth",
                                 description="per-day provider-truth totals (keys stay local); --push syncs them to the org server")
    ap.add_argument("--since", help="YYYY-MM-DD (default: 35 days back)")
    ap.add_argument("--push", action="store_true", help="push to the org server (default: preview only)")
    a = ap.parse_args(sys.argv[2:] if argv is None else argv)
    rs = rows(a.since)
    total = sum(r["usd"] for r in rs)
    print(f"provider truth — {len(rs)} day-rows since {a.since or '(35d back)'}   Σ ${total:,.2f}")
    for r in rs[-14:]:
        print(f"  {r['day']}  {r['provider']:<10} ${r['usd']:>10,.2f}")
    if len(rs) > 14:
        print(f"  … ({len(rs) - 14} earlier rows)")
    if a.push:
        res = push(a.since)
        print(f"  push → {res}")
    else:
        print("  (preview only — pass --push to sync to the org server)")
    return 0

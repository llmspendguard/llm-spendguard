"""Reconcile the LOCAL gate ledger against the PROVIDER's actual billing — find leaks.

The gate's local SQLite ledger records what spendguard SAW (gated). The providers bill what actually
happened. Comparing them per day surfaces the thing that matters: **spend billed by the provider that the
gate never recorded** = ungoverned/leaked spend (calls from a non-gated venv/process/repo, or before the
local ledger existed). Provider batch usage is the ground truth (fetched free, no Admin key needed);
real-time spend isn't provider-visible without an Admin key, so it's shown local-only.

  spendguard reconcile-ledger            # this month
  spendguard reconcile-ledger --since 2026-06-01

LEAK (provider > local) = the important signal. Local > provider = estimate-over-actual / double-count.
"""
import argparse
import datetime


def _provider_batch_by_day(since):
    from .report import openai_by_day
    from . import reconcile_anthropic as anth
    prov = {}
    try:
        oai, pending = openai_by_day()
    except Exception:
        oai, pending = {}, 0
    try:
        an, _models = anth.cost_by_day(since=since)
    except Exception:
        an = {}
    for d, v in list(oai.items()) + list(an.items()):
        if d >= since:
            prov[d] = prov.get(d, 0.0) + v
    return prov, pending


def _compute(since=None):
    """Compute the local-vs-provider reconciliation WITHOUT printing — for the report/monitor."""
    from . import budget
    since = since or datetime.date.today().replace(day=1).isoformat()
    prov, pending = _provider_batch_by_day(since)
    local_batch = budget.by_day(kind="batch", since=since)
    cutoff = budget.ledger_start() or since
    post_p = sum(v for d, v in prov.items() if d >= cutoff)
    post_l = sum(v for d, v in local_batch.items() if d >= cutoff)
    leak = sum(max(0.0, prov.get(d, 0) - local_batch.get(d, 0))
               for d in prov if d >= cutoff and prov.get(d, 0) - local_batch.get(d, 0) > max(0.5, 0.05 * prov.get(d, 0)))
    return dict(since=since, cutoff=cutoff, prov=prov, local_batch=local_batch,
                local_rt=budget.by_day(kind="realtime", since=since), meta=budget.by_day(kind="meta", since=since),
                pending=pending, post_p=post_p, post_l=post_l, leak=leak,
                coverage=(post_l / post_p * 100) if post_p else 100.0)


def leak_line(since=None):
    """One-line leak alert for the report (or None if clean / nothing to compare)."""
    try:
        c = _compute(since)
    except Exception:
        return None
    if c["leak"] > 0.5:
        return (f"⚠️ LEDGER LEAK: ~${c['leak']:.2f} provider-billed batch not in the local ledger since "
                f"{c['cutoff']} (coverage {c['coverage']:.0f}%) — run `spendguard reconcile-ledger`.")
    if c["post_p"] > 0:
        return f"ledger coverage {c['coverage']:.0f}% of provider batch since {c['cutoff']} (no material leak)."
    return None


def sync(since=None):
    from . import budget
    since = since or datetime.date.today().replace(day=1).isoformat()
    prov, pending = _provider_batch_by_day(since)
    local_batch = budget.by_day(kind="batch", since=since)
    local_rt = budget.by_day(kind="realtime", since=since)
    meta = budget.by_day(kind="meta", since=since)
    lstart = budget.ledger_start()

    print(f"reconcile-ledger — local gate ledger vs provider billing, since {since}")
    if lstart and lstart > since:
        print(f"  ⚠ local ledger only has data since {lstart}; provider spend before that is pre-ledger "
              f"(expected gap, not a true leak).")
    print(f"\n  {'day':<12}{'provider batch':>15}{'local batch':>13}{'diff':>11}  status")
    days = sorted(set(prov) | set(local_batch))
    p_tot = l_tot = leak = post_p = post_l = 0.0
    cutoff = lstart or since
    for d in days:
        p, l = prov.get(d, 0.0), local_batch.get(d, 0.0)
        diff = p - l
        p_tot += p; l_tot += l
        pre = d < cutoff
        if pre:
            status = "· pre-ledger (expected)"
        else:
            post_p += p; post_l += l
            if diff > max(0.5, 0.05 * p):
                status, leak = "⚠️ LEAK (billed, not gated)", leak + diff
            elif diff < -max(0.5, 0.05 * max(p, l)):
                status = "over-recorded (est>actual?)"
            else:
                status = "ok"
        print(f"  {d:<12}{('$%.2f' % p):>15}{('$%.2f' % l):>13}{('$%+.2f' % diff):>11}  {status}")
    print(f"  {'TOTAL':<12}{('$%.2f' % p_tot):>15}{('$%.2f' % l_tot):>13}{('$%+.2f' % (p_tot - l_tot)):>11}")
    cov = (post_l / post_p * 100) if post_p else 100.0
    print(f"\n  since the ledger went live ({cutoff}): provider ${post_p:.2f} vs local ${post_l:.2f} "
          f"→ coverage {cov:.0f}%")
    if leak > 0.5:
        print(f"  ⚠ ~${leak:.2f} provider-billed batch since {cutoff} is NOT in the local ledger — "
              f"ungoverned spend (a non-gated venv/process/repo). Install the gate there.")
    elif post_p > 0:
        print("  ✓ no material leak since the ledger went live — provider batch billing is accounted for.")
    else:
        print("  (no provider batch billing since the ledger went live yet — re-run after the next gated batch.)")
    print(f"  real-time (local-only, no provider cross-check w/o Admin key): ${sum(local_rt.values()):.2f}")
    print(f"  spendguard meta (advisor): ${sum(meta.values()):.2f}")
    if pending:
        print(f"  ({pending:,} OpenAI requests in flight — not yet billed)")
    return dict(provider=p_tot, local=l_tot, coverage=cov, leak=leak)


def main(argv=None):
    ap = argparse.ArgumentParser(prog="spendguard reconcile-ledger")
    ap.add_argument("--since", help="YYYY-MM-DD (default: start of this month)")
    a = ap.parse_args(argv)
    sync(since=a.since)
    return 0

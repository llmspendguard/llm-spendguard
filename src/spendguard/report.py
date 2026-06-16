"""Unified LLM + Remote Compute spend report — OpenAI (gpt-5.5 etc.) + Anthropic (Opus 4.8 etc.) + vast.ai GPU.

Reports DAILY / WEEKLY / MONTHLY billed batch spend per provider, an LLM subtotal, remote-compute (vast.ai GPU)
spend, and a grand total — priced via canonical pricing.py. Message-ready for the scheduled monitor.
ZERO paid calls (batch metadata + result GETs + vast.ai instance GETs only).

  python scripts/spend_report.py
  python scripts/spend_report.py --alert-threshold 150   # adds an ALERT line if TODAY total (LLM+compute) > $150

Scope: BATCH spend both providers + GPU consumption from currently-visible vast.ai instances. Real-time LLM spend
(e.g. Opus LOINC judge) needs an Admin key — noted in output; destroyed GPU instances aren't per-instance billed.
"""
import os, sys, argparse, datetime
from collections import defaultdict

from . import pricing
from .reconcile_openai import load_key, fetch_batches, day as oai_day
from . import reconcile_anthropic as anth
def openai_by_day():
    by_day = defaultdict(float)
    pending = 0
    for b in fetch_batches(load_key()):
        if b["status"] in ("in_progress", "finalizing", "validating"):
            pending += b["request_counts"]["total"]; continue
        if b["status"] not in ("completed", "cancelled"):
            continue
        u = b.get("usage") or {}
        it, ot = u.get("input_tokens", 0), u.get("output_tokens", 0)
        if not it and not ot:
            continue
        c = pricing.batch_cost(b["model"], it, ot, (u.get("input_tokens_details") or {}).get("cached_tokens", 0))
        by_day[oai_day(b)] += c
    return by_day, pending


def gpu_by_day(since):
    """{day: $} of vast.ai GPU consumption from currently-visible instances (per-day split), for the report's
    Remote-Compute row. Free (instance GETs). Returns ({}, note) on any vast.ai error so the report still renders.
    NB: destroyed instances aren't per-instance billed by vast.ai → this is current-instance consumption, not the
    account top-up total (which is lumpy + cross-org on a shared account)."""
    import datetime
    try:
        from . import resources
        since_ts = datetime.datetime.strptime(since, "%Y-%m-%d").replace(tzinfo=datetime.timezone.utc).timestamp()
        bd = defaultdict(float)
        for r in resources.gpu_rows_by_day(since_ts=since_ts):
            bd[r["day"]] += r["cost"]
        return dict(bd), None
    except Exception as e:
        return {}, str(e)[:80]


def windows(today):
    month_start = today.replace(day=1).strftime("%Y-%m-%d")
    week_start = (today - datetime.timedelta(days=6)).strftime("%Y-%m-%d")
    tstr = today.strftime("%Y-%m-%d")
    return tstr, week_start, month_start


def sum_window(by_day, lo, hi=None):
    return sum(v for d, v in by_day.items() if d >= lo and (hi is None or d <= hi))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--alert-threshold", type=float, help="ALERT if TODAY combined exceeds this $")
    ap.add_argument("--email", action="store_true", help="also email the report (SMTP config required)")
    ap.add_argument("--email-to", help="recipient override (else SPENDGUARD_EMAIL_TO / ~/.spendguard/email.json)")
    a = ap.parse_args()

    import io, contextlib
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = _run(a)
    text = buf.getvalue()
    print(text, end="")
    if a.email:
        from . import notify
        if not notify.is_configured():
            print("  email not configured — skipping (set up a sender: see README → 'Email the report')")
        else:
            stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")
            subj = f"LLM and Remote Compute spend report — {stamp}" + ("  ⚠️ ALERT" if rc == 2 else "")
            try:
                to = notify.send_email(subj, text, to=a.email_to)
                print(f"  (emailed to {to})")
            except Exception as e:
                print(f"  EMAIL FAILED: {e}")
    # roll up to the team/org server if connected + due (respects saas.sync_interval; safe no-op otherwise)
    try:
        from . import saas
        if saas.ready()[0]:
            print(f"  saas sync: {saas.sync(if_due=True)}")
    except Exception:
        pass
    return rc


def _run(a):
    today = datetime.datetime.now(datetime.timezone.utc).date()
    tstr, week_start, month_start = windows(today)

    oai, pending = openai_by_day()
    an, an_models = anth.cost_by_day(since=month_start)  # only need this month onward for the windows
    from . import gate
    rt, _rt_models = gate.realtime_by_day(since=month_start)  # real-time spend the gate logged

    def row(name, bd):
        return (name, sum_window(bd, tstr), sum_window(bd, week_start), sum_window(bd, month_start))

    gpu, gpu_err = gpu_by_day(month_start)               # remote compute (vast.ai GPU) — free instance GETs

    r_oai = row("OpenAI batch (gpt-5.5)", oai)
    r_an = row("Anthropic batch (Opus)", an)
    r_rt = row("Real-time (gate-logged)", rt)
    llm_sub = ("LLM subtotal", r_oai[1] + r_an[1] + r_rt[1], r_oai[2] + r_an[2] + r_rt[2], r_oai[3] + r_an[3] + r_rt[3])
    r_gpu = row("Remote compute (vast.ai GPU)", gpu)
    total = ("TOTAL (LLM + compute)", llm_sub[1] + r_gpu[1], llm_sub[2] + r_gpu[2], llm_sub[3] + r_gpu[3])
    combined = total                                     # the alert threshold tracks the grand total (LLM + compute)

    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%MZ")
    print(f"LLM AND REMOTE COMPUTE SPEND REPORT — {stamp}  (priced via pricing.py {pricing.PRICING_VERIFIED})")
    print(f"{'source':<30}{'today':>11}{'last 7d':>12}{'month':>12}")
    for name, t, w, m in (r_oai, r_an, r_rt, llm_sub, r_gpu, total):
        if name == "Remote compute (vast.ai GPU)":
            print("  " + "-" * 61)                       # divider: LLM subtotal above, compute below
        print(f"{name:<30}{'$%.2f'%t:>11}{'$%.2f'%w:>12}{'$%.2f'%m:>12}")
    if gpu_err:
        print(f"  (remote compute: vast.ai unreachable — {gpu_err}; GPU shown as $0 this run)")
    else:
        print("  remote compute = consumption from currently-visible vast.ai instances (destroyed instances aren't per-instance billed).")
    if an_models:
        print("  Anthropic batch by model (month): " + ", ".join(f"{k.split('-')[1] if '-' in k else k}:${v:.0f}" for k, v in sorted(an_models.items(), key=lambda x: -x[1])))
    if pending:
        print(f"  ({pending:,} OpenAI requests in flight — not yet metered)")
    print("  NOTE: real-time = only calls made through the gate (this venv); other-host real-time still needs an Admin key.")
    from . import budget
    mt, mw, mm = (budget.meta_spent_since(tstr), budget.meta_spent_since(week_start), budget.meta_spent_since(month_start))
    if mt or mw or mm:
        print(f"{'spendguard meta (advisor)':<30}{'$%.2f' % mt:>11}{'$%.2f' % mw:>12}{'$%.2f' % mm:>12}   (own cap; not in TOTAL)")
    _v, _days, _stale = pricing.freshness()
    if _stale:
        print(f"  ⚠️ PRICE TABLE STALE: verified {_v} ({_days}d ago). Re-verify vs {pricing.PRICING_SOURCE} and update prices.json (`spendguard check-prices`).")
    if anth.UNKNOWN_MODELS:
        print("  WARN Anthropic models missing from pricing.py (priced $0): "
              + ", ".join(f"{m}×{n}" for m, n in anth.UNKNOWN_MODELS.items()))

    # ── ledger leak check (provider billing vs what the gate recorded) ──
    leaked = 0.0
    try:
        from . import ledger_sync
        c = ledger_sync._compute(month_start)
        leaked = c["leak"]
        line = ledger_sync.leak_line(month_start)
        if line:
            print("  " + line)
    except Exception:
        pass

    # ── top learnings (the advisor's confidence-scored insights) ──
    try:
        from . import learn
        ins = learn.insights(min_conf=0.7)
        if ins:
            print("\nTOP LEARNINGS (spendguard advisor):")
            for intent, lesson, src, conf, _ev in ins[:6]:
                print(f"  [{conf:.2f}] {lesson[:100]}")
            print("  (full: `spendguard insights list` · recommend: `spendguard optimize --intent <X>`)")
    except Exception:
        pass

    rc = 0
    if a.alert_threshold and combined[1] > a.alert_threshold:
        print(f"\n*** ALERT: today total (LLM + compute) ${combined[1]:,.2f} exceeds ${a.alert_threshold:,.0f}. "
              f"Check `spendguard reconcile openai|anthropic --by-day` + `spendguard resources`. ***")
        rc = 2
    if leaked > max(1.0, (a.alert_threshold or 1e9) * 0.1):
        print(f"\n*** ALERT: ~${leaked:.2f} provider-billed batch is NOT in the local ledger (ungoverned). "
              f"Run `spendguard reconcile-ledger`; install the gate on any repo/venv that's missing it. ***")
        rc = max(rc, 2)
    return rc


if __name__ == "__main__":
    sys.exit(main())

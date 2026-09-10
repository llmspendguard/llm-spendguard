"""Time each step of `spendguard report` in isolation to locate the runaway (the daily cron took ~70 min).

READ-ONLY: calls only the report's fetch/read functions — NO email, NO org push, NO LLM auto_fresh, NO watermark
writes. The network GETs (OpenAI/Anthropic batch listing, vast.ai instances) are $0. Prints wall-seconds per step
so the dominant cost is measured, not guessed. Run under the gated venv:

    .venv.nosync/bin/python scripts/probe/report_timing.py
"""
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "src"))

from spendguard import report, reconcile_anthropic as anth, gate, budget, ledger_sync, learn, realized, config  # noqa: E402


def timed(label, fn):
    t0 = time.time()
    err = None
    try:
        fn()
    except Exception as e:                                  # a step that errors is still a data point
        err = f"{type(e).__name__}: {str(e)[:90]}"
    dt = time.time() - t0
    print(f"  {dt:8.2f}s  {label}" + (f"   ERR {err}" if err else ""))
    return dt


def main():
    today = __import__("datetime").datetime.now(__import__("datetime").timezone.utc).date()
    tstr, week_start, month_start = report.windows(today)
    print(f"report step timing (month_start={month_start}) — read-only, $0\n")
    total = 0.0
    total += timed("openai_by_day(month)  [BOUNDED: page-stops at the window]", lambda: report.openai_by_day(since=month_start))
    total += timed("anth.cost_by_day(month)  [cache-backed, incremental]", lambda: anth.cost_by_day(since=month_start))
    total += timed("gate.realtime_by_day(month)  [RT_LOG file]", lambda: gate.realtime_by_day(since=month_start))
    total += timed("gpu_by_day(month)  [vast.ai, network]", lambda: report.gpu_by_day(month_start))
    total += timed("meta_spent_since x3  [ledger]", lambda: [budget.meta_spent_since(x) for x in (tstr, week_start, month_start)])
    total += timed("ledger_sync._compute(month)  [bounded re-fetch]", lambda: ledger_sync._compute(month_start))
    total += timed("learn.insights(0.7)  [ledger]", lambda: learn.insights(min_conf=0.7))
    total += timed("realized.measure()  [calls table, read-only]", lambda: realized.measure())
    print(f"\n  {total:8.2f}s  TOTAL (sequential — the report now runs the 3 provider pulls CONCURRENTLY)")
    print("\n-- the report's ACTUAL concurrent fetch (openai ∥ anth ∥ gpu) --")
    timed("_fetch_sources_concurrently(month)  [what generate_report now calls]", lambda: report._fetch_sources_concurrently(month_start))
    print("  (NOT timed here — write/LLM side-effects the real report also runs: review.auto_fresh [LLM],")
    print("   calibrate.pair/push_shared/fetch_shared [org network+writes], realized.sync_to_guarded [writes],")
    print("   saas.sync, notify.send_email)")


if __name__ == "__main__":
    main()

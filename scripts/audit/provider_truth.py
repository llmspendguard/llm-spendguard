"""Authoritative provider GROUND TRUTH for a month, by provider × channel — the anchor the whole billed tally MUST
reconcile to (Σ attributed ≤ this, batch diff ≈ $0). Zero spend (provider GETs are free).

  BATCH    = the provider batch APIs   — openai_by_day() + reconcile_anthropic.cost_by_day()  (derivation source)
  REALTIME = the ADMIN usage APIs      — report.admin_realtime_total()  (admin cross-check only; needs admin keys)
  GPU      = vast.ai                   — report.gpu_by_day()

Usage: python scripts/audit/provider_truth.py [SINCE=2026-06-01]
"""
import sys

from spendguard import report, reconcile_anthropic


def main():
    since = sys.argv[1] if len(sys.argv) > 1 else "2026-06-01"

    oai_batch, pending = report.openai_by_day()
    oai_b = report.sum_window(oai_batch, since)

    anth = reconcile_anthropic.cost_by_day(since=since)
    anth_by_day = anth[0] if isinstance(anth, tuple) else anth
    anth_b = sum(v for d, v in anth_by_day.items() if d >= since)

    rt = report.admin_realtime_total(since=since)        # AUTHORITATIVE realtime via admin keys (cross-check)

    gpu, note = report.gpu_by_day(since)
    gpu_m = report.sum_window(gpu, since)

    print(f"PROVIDER GROUND TRUTH since {since}")
    print(f"  OpenAI batch (batch API):        ${oai_b:,.2f}")
    print(f"  Anthropic batch (batch API):     ${anth_b:,.2f}")
    print(f"  Realtime OpenAI+Anthropic (ADMIN): " + (f"${rt:,.2f}" if rt is not None else "— NO ADMIN KEY (set OPENAI_ADMIN_KEY / ANTHROPIC_ADMIN_KEY)"))
    print(f"  GPU vast.ai:                     ${gpu_m:,.2f}" + (f"   (note: {note})" if note else ""))
    billed = oai_b + anth_b + (rt or 0) + gpu_m
    print(f"  ─────────────────────────────")
    print(f"  TOTAL BILLED $ (authoritative):  ${billed:,.2f}")
    print(f"    LLM = ${oai_b + anth_b + (rt or 0):,.2f}  ·  GPU = ${gpu_m:,.2f}  ·  pending batches: {pending}")


if __name__ == "__main__":
    main()

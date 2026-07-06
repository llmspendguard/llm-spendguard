"""Daily spend ANOMALY detection — the automated version of the gut check that caught both 2× P0s.

Both double-count crises were spotted by a human staring at a total that "felt about 2× of what it
should be." This module institutionalizes that: each source's TODAY total is z-scored against its own
trailing history using median/MAD (robust — yesterday's legitimate spike doesn't poison today's
baseline), and breaches print as ANOMALY lines in the daily report (and therefore in the emailed copy).

Pure functions over the report's existing by-day maps ({YYYY-MM-DD: usd}) — no new data plumbing, no
network, unit-testable. Thresholds err toward SIGNAL (z≥3.5, ≥$5, ≥1.5× median): a missed anomaly cost
us days; a rare false ANOMALY line costs a glance. The synthesized TOTAL series additionally catches a
spike hiding in a source too new to judge on its own history.
"""

MIN_HISTORY_DAYS = 7      # fewer prior active days than this → not enough baseline to judge
Z_THRESHOLD = 3.5         # robust z (median/MAD); 3.5 ≈ "not plausibly normal variation"
FLOOR_USD = 5.0           # never flag penny-scale days
MAD_SCALE = 1.4826        # MAD → σ-equivalent under normality


def robust_z(history, value):
    """z-score of `value` against `history` (list of $/day) via median/MAD. Returns (z, median).
    Degenerate histories: MAD 0 with a flat median → any meaningful jump reads as a large z (99.0)."""
    hist = sorted(history)
    n = len(hist)
    if not n:
        return 0.0, 0.0
    med = hist[n // 2] if n % 2 else (hist[n // 2 - 1] + hist[n // 2]) / 2
    dev = sorted(abs(x - med) for x in hist)
    mad = dev[n // 2] if n % 2 else (dev[n // 2 - 1] + dev[n // 2]) / 2
    if mad > 0:
        return (value - med) / (MAD_SCALE * mad), med
    return (99.0 if value > med else 0.0), med   # flat history: any rise above it is maximally surprising


def flag_today(by_day, today):
    """None, or {'usd','z','median','days'} when TODAY's total is anomalous vs this source's own history."""
    value = float(by_day.get(today, 0.0))
    history = [float(v) for d, v in by_day.items() if d != today]
    if value < FLOOR_USD or len(history) < MIN_HISTORY_DAYS:
        return None
    z, med = robust_z(history, value)
    if z >= Z_THRESHOLD and (value - med) >= max(FLOOR_USD, 0.5 * med):   # statistically wild AND ≥1.5× median
        # (both P0s were ~1.8–2× systematic inflation; a 2× gate would have hidden them — excess-over-median
        #  with a $ floor catches the real shape without flagging routine wobble)
        return {"usd": value, "z": z, "median": med, "days": len(history)}
    return None


def lines(named_series, today):
    """ANOMALY report lines for {name: by_day_map} — includes a synthesized TOTAL series. The exact
    failure both P0s exhibited (a doubled total) trips the TOTAL line even when each source looks tame."""
    series = dict(named_series)
    days = {d for m in named_series.values() for d in m}
    series["TOTAL"] = {d: sum(float(m.get(d, 0.0)) for m in named_series.values()) for d in days}
    out = []
    for name, by_day in series.items():
        f = flag_today(by_day, today)
        if f:
            out.append(f"  ANOMALY {name}: today ${f['usd']:,.2f} is z={f['z']:.1f} vs {f['days']}d history "
                       f"(median ${f['median']:,.2f}) — verify against provider totals before trusting ANY rollup")
    return out

"""Guards for the subscription-economics model (lane_economics) and the absolute-token water-filling it feeds into
route_utility.lane_score. These lock the two properties that make the feature correct and un-regressable:

  1. the token CAP is recovered from real consumption-vs-gauge samples, with no window length assumed, and stays
     None until a clean sample pair exists (never an invented cap);
  2. ranking water-fills on ABSOLUTE tokens where the cap is measured (a big-cap plan at X% outranks a small-cap
     plan at the same X%), and falls back EXACTLY to the old percentage order when no cap is known yet.
"""
from spendguard import lane_economics as le
from spendguard import route_utility as ru


def _samples(pct_tok, reset_ts=1000.0):
    """Build a sample series from [(remaining_pct, cumulative_tokens), …], all in ONE window (same reset_ts)."""
    return [{"ts": float(i), "remaining_pct": float(p), "reset_ts": reset_ts, "cum_tok": int(t)}
            for i, (p, t) in enumerate(pct_tok)]


def test_estimate_cap_recovers_known_cap(monkeypatch):
    # True cap 1,000,000 tok: 100→90% burned 100k tokens; 90→80% burned another 100k. C = 100k / 0.10 = 1M.
    monkeypatch.setattr(le, "_bucket_samples", lambda lane, b: _samples([(100, 0), (90, 100_000), (80, 200_000)]))
    cap, n = le.estimate_cap("gemini", "wk")
    assert n == 2
    assert abs(cap - 1_000_000) < 1.0


def test_cap_none_until_a_pair_exists(monkeypatch):
    monkeypatch.setattr(le, "_bucket_samples", lambda lane, b: _samples([(100, 0)]))     # one sample → no pair yet
    cap, n = le.estimate_cap("gemini", "wk")
    assert cap is None and n == 0


def test_cross_reset_pair_is_skipped(monkeypatch):
    # A refill (reset_ts changes 1000→2000, gauge jumps back up) must not read as consumption.
    s = [{"ts": 0, "remaining_pct": 50, "reset_ts": 1000, "cum_tok": 0},
         {"ts": 1, "remaining_pct": 40, "reset_ts": 1000, "cum_tok": 50_000},    # valid: 50k / 0.10 = 500k
         {"ts": 2, "remaining_pct": 95, "reset_ts": 2000, "cum_tok": 60_000}]    # cross-reset: skipped
    monkeypatch.setattr(le, "_bucket_samples", lambda lane, b: s)
    cap, n = le.estimate_cap("g", "wk")
    assert n == 1 and abs(cap - 500_000) < 1.0


def test_tiny_gauge_move_ignored(monkeypatch):
    # A 0.5% drop is below the noise floor — dividing by such a Δfrac would fabricate a wild cap.
    monkeypatch.setattr(le, "_bucket_samples", lambda lane, b: _samples([(100, 0), (99.5, 1000)]))
    cap, n = le.estimate_cap("g", "wk")
    assert cap is None and n == 0


def test_economics_derives_remaining_abs_and_eff(monkeypatch):
    # cap 1M (0→50% burned 500k); at 50% left → 500k tokens remain; weekly bucket → fee prorated 7/30.
    monkeypatch.setattr(le, "_bucket_samples", lambda lane, b: _samples([(100, 0), (50, 500_000)]))
    rows = [{"lane": "gemini", "provider": "google", "known": True, "remaining_pct": 50,
             "buckets": [{"bucket": "Gemini Weekly", "remaining_pct": 50, "reset_ts": 1000}]}]
    e = le.economics(headroom_rows=rows, fee_by_lane={"gemini": 100.0})
    assert len(e) == 1 and e[0]["converged"]
    b = e[0]["binding"]
    assert abs(b["cap"] - 1_000_000) < 1.0
    assert abs(b["remaining_abs"] - 500_000) < 1.0
    assert abs(b["used_abs"] - 500_000) < 1.0
    assert b["period_days"] == 7.0
    # eff $/tok = (fee * 7/30) / cap  = (100*7/30)/1e6
    assert abs(b["eff_usd_per_tok"] - (100.0 * 7.0 / 30.0) / 1_000_000) < 1e-12
    # fee at risk = remaining_abs * eff
    assert abs(b["waste_at_reset"] - 500_000 * b["eff_usd_per_tok"]) < 1e-9


def test_unknown_quota_lane_yields_no_economics_row(monkeypatch):
    monkeypatch.setattr(le, "_bucket_samples", lambda lane, b: [])
    rows = [{"lane": "zai-coding", "provider": "zai", "known": False, "remaining_pct": None, "buckets": []}]
    assert le.economics(headroom_rows=rows, fee_by_lane={"zai-coding": 100.0}) == []


def test_waterfill_bigger_cap_outranks_same_pct(monkeypatch):
    # Both lanes 30% left, but A has 3M tokens left vs B's 300k → A must rank first (more real capacity).
    rows = [{"lane": "B", "provider": "p", "known": True, "remaining_pct": 30, "reset_ts": None, "remaining_abs": 300_000},
            {"lane": "A", "provider": "p", "known": True, "remaining_pct": 30, "reset_ts": None, "remaining_abs": 3_000_000}]
    ranked = [r["lane"] for r in ru.rank_lanes(rows, cooling=lambda ln: False)]
    assert ranked[0] == "A"


def test_waterfill_falls_back_to_pct_when_no_cap():
    # No measured cap anywhere → the % fraction drives, IDENTICAL to the pre-feature behaviour.
    rows = [{"lane": "X", "provider": "p", "known": True, "remaining_pct": 80, "reset_ts": None},
            {"lane": "Y", "provider": "p", "known": True, "remaining_pct": 20, "reset_ts": None}]
    ranked = [r["lane"] for r in ru.rank_lanes(rows, cooling=lambda ln: False)]
    assert ranked[0] == "X"


def test_lane_score_stays_free_tier_above_metered():
    # Even the smallest lane headroom scores >= 1.0 (a plan token is free), so it outranks any metered target (<1).
    row = {"lane": "L", "provider": "p", "known": True, "remaining_pct": 1, "reset_ts": None, "remaining_abs": 10}
    assert ru.lane_score(row, abs_norm=1_000_000) >= 1.0

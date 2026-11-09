from unittest import mock

from spendguard.analyze.reconciliation import cost_integrity


def guard_healthy_and_impossible_cost_buckets_diverge():
    healthy = ("zero-lane", "day", "realtime", 1, 0.0, 0, 0, 0, 0)
    impossible = ("metered", "day", "realtime", 1, 1.0, 0, 0, 0, 0)
    with (mock.patch.object(cost_integrity.pricing_accessors, "priced_model_rates",
                           return_value={"in_": 1.0, "out": 1.0, "cached_in": 1.0, "cache_write": 1.0}),
          mock.patch.object(cost_integrity.pricing_accessors, "cache_write_5m_multiplier", return_value=1.0)):
        healthy_findings, healthy_unpriced = cost_integrity.classify_impossible_cost_buckets([healthy])
        impossible_findings, impossible_unpriced = cost_integrity.classify_impossible_cost_buckets([impossible])
    assert healthy_findings == [] and healthy_unpriced == 0
    assert len(impossible_findings) == 1 and impossible_unpriced == 0


if __name__ == "__main__":
    guard_healthy_and_impossible_cost_buckets_diverge()
    print("GREEN: healthy zero-cost bucket passes and impossible nonzero cost fails")

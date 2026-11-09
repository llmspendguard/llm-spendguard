"""Doctor pricing UNKNOWNs are named and do not weaken enforcement exits."""
from unittest.mock import patch

from spendguard.analyze.reconciliation import cost_integrity


COSTLESS_MODEL = "costless-catalogue-fixture"
COSTLESS_DAY = "fixture-day"
COSTLESS_ROW = (COSTLESS_MODEL, COSTLESS_DAY, "realtime", 1, 0.0, 1, 0)


def test_costless_catalogue_row_uses_existing_named_unknown_reporter_without_traceback():
    with patch.object(cost_integrity, "_max_token_cost", return_value=None), \
            patch.object(cost_integrity.COST_INTEGRITY_LOGGER, "exception") as traceback_logger:
        gaps = []
        findings, unpriced = cost_integrity.classify_impossible_cost_buckets([COSTLESS_ROW], gaps=gaps)

    assert findings == [] and unpriced == 1
    assert not traceback_logger.called
    audit_result = {"ok": True, "findings": findings, "unpriced": unpriced,
                    "checked": 1, "gaps": gaps}
    with patch.object(cost_integrity, "audit_metered_cost_vs_tokens", return_value=audit_result):
        rendered = "\n".join(cost_integrity.cost_integrity_lines())
    assert "⚪ UNKNOWN" in rendered and COSTLESS_MODEL in rendered
    assert "Traceback" not in rendered and "ValueError" not in rendered


test_costless_catalogue_row_uses_existing_named_unknown_reporter_without_traceback()
print("[OK] costless catalogue entries use the named UNKNOWN contract")

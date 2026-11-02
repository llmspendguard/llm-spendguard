"""Exercise readiness against real temporary tripwire ledgers, never operator state."""
from pathlib import Path
import tempfile
from unittest.mock import patch

from test_protected_lane_leak_tripwire import (
    OTHER_EXECUTOR, OTHER_PROVIDER, PROTECTED_EXECUTOR, PROTECTED_PROVIDER,
    TEST_COST, create_tripwire_ledger, tripwire_row,
)
from spendguard import capability_readiness_gates
from spendguard.capability_readiness import load_capability_readiness_manifest

READINESS_FIXTURE_WINDOW_HOURS = 1.0


def run_lane_leak_readiness_fixture_contract():
    _, readiness_items = load_capability_readiness_manifest()
    armed_item = next(item for item in readiness_items if item.id == "lane-leak-tripwire")
    assert armed_item.gate == (
        "spendguard.capability_readiness_gates:assess_protected_lane_leak_readiness")
    assert armed_item.arguments == {"hours": READINESS_FIXTURE_WINDOW_HOURS}
    with tempfile.TemporaryDirectory(prefix="sg-leak-readiness-") as directory:
        fixture_root = Path(directory)
        cases = (
            ("metered", [tripwire_row(provider=PROTECTED_PROVIDER, executor="api", cost=TEST_COST)],
             "FAIL", "REAL DOLLARS"),
            ("clean", [tripwire_row(provider=OTHER_PROVIDER, executor=OTHER_EXECUTOR)], "PASS", "CLEAN:"),
            ("quiet", [], "UNREVIEWED", "INCONCLUSIVE:"),
        )
        with patch("spendguard.config.shed_lanes", return_value={PROTECTED_EXECUTOR}), patch(
                "spendguard.lane_catalog.lane_provider", return_value=PROTECTED_PROVIDER):
            for case_name, rows, expected_status, expected_report in cases:
                fixture_path = fixture_root / f"{case_name}.sqlite"
                create_tripwire_ledger(fixture_path, rows)
                result = capability_readiness_gates.assess_protected_lane_leak_readiness(
                    context={}, hours=READINESS_FIXTURE_WINDOW_HOURS, ledger_path=fixture_path)
                assert result.status == expected_status, result
                assert expected_report in result.evidence[0], result
                assert f"hours={READINESS_FIXTURE_WINDOW_HOURS}" in result.evidence
                print(f"PASS: {case_name} -> {expected_status}")

            absent_path = fixture_root / "absent.sqlite"
            with patch("spendguard.config.db_path", return_value=absent_path):
                absent = capability_readiness_gates.assess_protected_lane_leak_readiness(context={})
            assert absent.status == "PASS" and "absent" in " ".join(absent.evidence)
            assert str(absent_path) in " ".join(absent.evidence)
            assert not absent_path.exists()
            print("PASS: absent -> PASS with honest absence evidence")

            broken_path = fixture_root / "broken.sqlite"
            broken_path.write_text("not a SQLite database", encoding="utf-8")
            broken = capability_readiness_gates.assess_protected_lane_leak_readiness(
                context={}, ledger_path=broken_path)
            assert broken.status == "UNREVIEWED", broken
            assert "PROBE FAILED:" in broken.evidence[0]
            print("PASS: broken -> UNREVIEWED")


if __name__ == "__main__":
    run_lane_leak_readiness_fixture_contract()

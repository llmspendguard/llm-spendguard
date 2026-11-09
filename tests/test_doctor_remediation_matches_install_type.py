from unittest import mock

from spendguard.platform import fleet
from spendguard.platform._version import __version__
from spendguard import pricing


def guard_wheel_remediation_matches_install_type():
    current = {"state": "noneditable_pinned", "code_version": __version__,
               "metadata_version": __version__, "catalog_verified": pricing.PRICING_VERIFIED}
    stale = {**current, "metadata_version": None}
    database = mock.MagicMock()
    database.execute.return_value.fetchall.return_value = [("consumer", "consumer-python")]
    database.__enter__.return_value = database
    with (mock.patch.object(fleet, "_fleet_db", return_value=database),
          mock.patch.object(fleet, "_consumer_install_state", return_value=current)):
        assert fleet.consumer_install_drift() == []
    with (mock.patch.object(fleet, "_fleet_db", return_value=database),
          mock.patch.object(fleet, "_consumer_install_state", return_value=stale)):
        drift = fleet.consumer_install_drift()
    assert len(drift) == 1
    assert " install -e " not in drift[0]["remediation"]
    assert " --upgrade " in drift[0]["remediation"]


if __name__ == "__main__":
    guard_wheel_remediation_matches_install_type()
    print("GREEN: current wheel is healthy; stale wheel receives wheel-valid remediation")

"""Global real dollars follow measured plan shares; estimated value never joins their sum.

Unequal scopes exercise project and team attribution. Missing basis fault injection must refuse.
"""
import os
import contextlib
import io
import tempfile
from math import isclose
from unittest.mock import patch

os.environ.setdefault("SPENDGUARD_HOME", tempfile.mkdtemp(prefix="sg-receipt-allocation-"))
os.environ.setdefault("SPENDGUARD_NO_AUTOINSTALL", "1")

from spendguard import budget, receipt  # noqa: E402

ALLOCATION_REPO_PRIMARY = "allocation-primary"
ALLOCATION_REPO_SECONDARY = "allocation-secondary"
ALLOCATION_PLAN_FEE = 160.0
ALLOCATION_REMOTE_COST = 80.0
ALLOCATION_EXTERNAL_COST = 40.0
ALLOCATION_API_COST = 12.0
ALLOCATION_USAGE_PRIMARY = 30.0
ALLOCATION_USAGE_SECONDARY = 90.0
ALLOCATION_MICROS_PER_DOLLAR = 1_000_000
ALLOCATION_WINDOWS = ("today", "week", "month")

allocation_day = receipt._windows()[0]
receipt.stamp_est_value([
    {"day": allocation_day, "billed": False, "project": ALLOCATION_REPO_PRIMARY,
     "spend_micros": ALLOCATION_USAGE_PRIMARY * ALLOCATION_MICROS_PER_DOLLAR},
    {"day": allocation_day, "billed": False, "team": ALLOCATION_REPO_SECONDARY,
     "spend_micros": ALLOCATION_USAGE_SECONDARY * ALLOCATION_MICROS_PER_DOLLAR},
])
allocation_remote = dict.fromkeys(ALLOCATION_WINDOWS, ALLOCATION_REMOTE_COST)
allocation_external = dict.fromkeys(ALLOCATION_WINDOWS, ALLOCATION_EXTERNAL_COST)
with patch.object(budget, "spent_since", return_value=ALLOCATION_API_COST), \
     patch.object(receipt, "_plan_usd", return_value=(ALLOCATION_PLAN_FEE, False)), \
     patch.object(receipt, "_remote_tally", return_value=allocation_remote), \
     patch.object(receipt, "_external_tally", return_value=allocation_external):
    allocation_global = receipt.tally()
    for allocation_repo, allocation_usage in (
            (ALLOCATION_REPO_PRIMARY, ALLOCATION_USAGE_PRIMARY),
            (ALLOCATION_REPO_SECONDARY, ALLOCATION_USAGE_SECONDARY)):
        allocation_share = allocation_usage / (ALLOCATION_USAGE_PRIMARY + ALLOCATION_USAGE_SECONDARY)
        allocation_scoped = receipt.tally(project=allocation_repo)
        assert isclose(allocation_scoped["subscription"], ALLOCATION_PLAN_FEE * allocation_share)
        for allocation_window in ALLOCATION_WINDOWS:
            assert isclose(allocation_scoped["remote"][allocation_window], ALLOCATION_REMOTE_COST * allocation_share)
            assert isclose(allocation_scoped["external"][allocation_window], ALLOCATION_EXTERNAL_COST * allocation_share)
            assert allocation_scoped["api"][allocation_window] == ALLOCATION_API_COST
        allocation_expected_real = ALLOCATION_API_COST + allocation_share * (
            ALLOCATION_PLAN_FEE + ALLOCATION_REMOTE_COST + ALLOCATION_EXTERNAL_COST)
        assert isclose(allocation_scoped["real_month"], allocation_expected_real)
        assert allocation_scoped["est_value"]["month"] == allocation_usage
        allocation_rendered = receipt.render_line(allocation_scoped)
        allocation_real_text, allocation_value_text = allocation_rendered.split("  ::  ", maxsplit=1)
        assert f"real {receipt._k(allocation_expected_real)}/mo" in allocation_real_text
        for allocation_label, allocation_component in (
                ("API", ALLOCATION_API_COST), ("subs", ALLOCATION_PLAN_FEE * allocation_share),
                ("remote", ALLOCATION_REMOTE_COST * allocation_share),
                ("external", ALLOCATION_EXTERNAL_COST * allocation_share)):
            assert f"{allocation_label} {receipt._k(allocation_component)}" in allocation_real_text
        assert f"est value {receipt._k(allocation_usage)}/mo" in allocation_value_text
    assert allocation_global["subscription"] == ALLOCATION_PLAN_FEE
    assert allocation_remote == dict.fromkeys(ALLOCATION_WINDOWS, ALLOCATION_REMOTE_COST)
    assert allocation_external == dict.fromkeys(ALLOCATION_WINDOWS, ALLOCATION_EXTERNAL_COST)
    with patch.object(budget, "_project", return_value=ALLOCATION_REPO_PRIMARY), \
         patch("spendguard.lane_value.refresh_lane_value_if_stale"), \
         patch("spendguard.claudecode.residency_summary_lines", return_value=[]):
        allocation_cli_output = io.StringIO()
        with contextlib.redirect_stdout(allocation_cli_output):
            assert receipt.cli([]) == 0
        allocation_cli_text = allocation_cli_output.getvalue()
        allocation_expected_sub = ALLOCATION_PLAN_FEE * ALLOCATION_USAGE_PRIMARY / (
            ALLOCATION_USAGE_PRIMARY + ALLOCATION_USAGE_SECONDARY)
        assert f"[{ALLOCATION_REPO_PRIMARY}]" in allocation_cli_text
        allocation_sub_line = next(row for row in allocation_cli_text.splitlines() if "Subscription" in row)
        assert receipt._money(allocation_expected_sub) in allocation_sub_line
        assert ALLOCATION_REPO_SECONDARY not in allocation_cli_text
        allocation_cli_output = io.StringIO()
        with contextlib.redirect_stdout(allocation_cli_output):
            assert receipt.cli(["--all"]) == 0
        assert receipt._money(ALLOCATION_PLAN_FEE) in allocation_cli_output.getvalue()
    with patch.object(receipt, "_est_tally", return_value=None):
        try:
            receipt.tally(project=ALLOCATION_REPO_PRIMARY)
        except ValueError as allocation_failure:
            assert "missing or stale plan-usage basis" in str(allocation_failure)
            assert ALLOCATION_REPO_PRIMARY in str(allocation_failure)
        else:
            raise AssertionError("Missing plan-usage basis silently allocated global money")
print("PASS: contextual global-dollar allocation and missing-basis fault refusal")

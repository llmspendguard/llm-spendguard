"""Offline ledger regression: text money, overflow families, and unattributed value all count."""
import os
import sys
import tempfile

if not os.environ.get("SPENDGUARD_TEST_ISOLATED"):
    os.environ["SPENDGUARD_TEST_ISOLATED"] = "1"
    os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="spendguard-estvalue-")
    os.execv(sys.executable, [sys.executable] + sys.argv)

from spendguard import budget, config, receipt  # noqa: E402
from spendguard.sources import (  # noqa: E402
    CLAUDE_CODE_SOURCE, OVERFLOW_SOURCE_SUFFIX, UNATTRIBUTED_EST_SOURCE,
)

ESTVALUE_TEXT_FIXTURES = (
    (CLAUDE_CODE_SOURCE, "12.50"),
    (CLAUDE_CODE_SOURCE + OVERFLOW_SOURCE_SUFFIX, "7.25"),
    (CLAUDE_CODE_SOURCE + OVERFLOW_SOURCE_SUFFIX + "-tagged", "1.75"),
    ("unregistered-fixture-source", "3.50"),
    (None, "0.25"),
    ("", "0.25"),
)
ESTVALUE_EXPECTED_LANE = 21.5
ESTVALUE_EXPECTED_OTHER = 4.0
ESTVALUE_TEST_TOLERANCE = 1e-9
ESTVALUE_FIXTURE_REPO = "fixture-repo"
ESTVALUE_INVALID_TEXT = ("", "not-a-number", None)


def check_receipt_estvalue_fixture(name, condition, failures):
    print(("[OK] " if condition else "[FAIL] ") + name)
    if not condition:
        failures.append(name)


def run_receipt_estvalue_checks(failures):
    fixture_day = receipt._windows()[0]
    for fixture_index, (fixture_source, fixture_amount) in enumerate(ESTVALUE_TEXT_FIXTURES):
        # Same canonical fixture writer used in test_receipt.py; money is stored as TEXT by the ledger.
        budget._record_spend_event(
            provider="fixture", model=None, kind="est_chat", cost=fixture_amount,
            source=fixture_source, dedup_key=f"estvalue-fixture-{fixture_index}",
            occurred_at=fixture_day + "T12:00:00+00:00", project=ESTVALUE_FIXTURE_REPO)

    import contextlib
    import sqlite3

    with contextlib.closing(sqlite3.connect(config.db_path())) as fixture_conn:
        storage_types = fixture_conn.execute("SELECT typeof(est_chat_usd) FROM spend_events").fetchall()
        for fixture_index, invalid_value in enumerate(ESTVALUE_INVALID_TEXT):
            # Preserve a different money column while exercising corrupt/empty legacy est text.
            fixture_conn.execute(
                "INSERT INTO spend_events (id, source, day, est_chat_usd, realtime_usd, project_primary) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (f"invalid-est-{fixture_index}", CLAUDE_CODE_SOURCE, fixture_day,
                 invalid_value, "1.00", ESTVALUE_FIXTURE_REPO))
        fixture_conn.commit()
    check_receipt_estvalue_fixture(
        "fixture money is TEXT", all(row[0] == "text" for row in storage_types), failures)

    # A stale source cache must not override ledger truth or add to it.
    receipt.stamp_est_value([{"day": fixture_day, "spend_micros": 1, "billed": False}], CLAUDE_CODE_SOURCE)
    lane_values = receipt._est_by_source()
    check_receipt_estvalue_fixture(
        "TEXT base plus overflow plus tagged overflow are all in the lane",
        abs(lane_values.get(CLAUDE_CODE_SOURCE, {}).get("month", 0) - ESTVALUE_EXPECTED_LANE)
        < ESTVALUE_TEST_TOLERANCE, failures)
    check_receipt_estvalue_fixture(
        "unknown source remains counted in other",
        abs(lane_values.get(UNATTRIBUTED_EST_SOURCE, {}).get("month", 0) - ESTVALUE_EXPECTED_OTHER)
        < ESTVALUE_TEST_TOLERANCE, failures)
    check_receipt_estvalue_fixture(
        "global est total includes other without stale-cache duplication",
        abs((receipt._est_tally() or {}).get("month", 0) - sum(
            (ESTVALUE_EXPECTED_LANE, ESTVALUE_EXPECTED_OTHER))) < ESTVALUE_TEST_TOLERANCE, failures)
    check_receipt_estvalue_fixture(
        "repo cells use the same complete ledger value",
        abs((receipt._est_tally(repo=ESTVALUE_FIXTURE_REPO) or {}).get("month", 0) - sum(
            (ESTVALUE_EXPECTED_LANE, ESTVALUE_EXPECTED_OTHER))) < ESTVALUE_TEST_TOLERANCE, failures)
    with contextlib.closing(sqlite3.connect(config.db_path())) as fixture_conn:
        preserved_real = fixture_conn.execute(
            "SELECT SUM(CAST(realtime_usd AS REAL)) FROM spend_events").fetchone()[0]
    check_receipt_estvalue_fixture(
        "invalid est text preserves the real-dollar column", preserved_real == 3.0, failures)
    receipt._routed_lane_value = lambda: {CLAUDE_CODE_SOURCE: 0.5}
    rendered_estvalue_table = "\n".join(receipt._two_axis_table({"est_value": receipt._est_tally()}))
    check_receipt_estvalue_fixture(
        "displayed lane uses full ledger instead of routed calls subset",
        f"{CLAUDE_CODE_SOURCE} {receipt._money(ESTVALUE_EXPECTED_LANE)}" in rendered_estvalue_table, failures)
    check_receipt_estvalue_fixture(
        "display explicitly names other including unknown, NULL and blank sources",
        f"{UNATTRIBUTED_EST_SOURCE} {receipt._money(ESTVALUE_EXPECTED_OTHER)}" in rendered_estvalue_table, failures)


estvalue_failures = []
run_receipt_estvalue_checks(estvalue_failures)
sys.exit(1 if estvalue_failures else 0)

"""Every cap/flow window survives an unrelated pending WAL writer."""
import math
import sqlite3
from contextlib import ExitStack
from unittest.mock import patch

from _pytest_fixture_bridge import align_test_startup_home, run_pytest_style

align_test_startup_home(prefix=__name__)

from spendguard import budget, calls, config, gate, ledger_read, ledger_writer
from spendguard.money_scale import NANO_USD_PER_USD

PROJECTION_TEST_AMOUNT = 1.0
PROJECTION_TEST_CAP = PROJECTION_TEST_AMOUNT * 4
PROJECTION_TEST_INTENT = "projection-seam-test"
PROJECTION_TEST_DAY = budget._utc().strftime("%Y-%m-%d")
PROJECTION_TEST_EVENT = dict(id=PROJECTION_TEST_INTENT, source=PROJECTION_TEST_INTENT,
    day=PROJECTION_TEST_DAY, realtime_usd=str(PROJECTION_TEST_AMOUNT),
    chain=PROJECTION_TEST_INTENT, status="posted")
PROJECTION_TEST_PENDING_SQL = "UPDATE spend_events SET realtime_usd=? WHERE id=?"
PROJECTION_TEST_CHECKPOINT_SQL = "PRAGMA wal_checkpoint(TRUNCATE)"
PROJECTION_TEST_CALL_SQL = """INSERT INTO calls
    (ts,intent,cost,cost_nano,settlement_complete) VALUES (?,?,?,?,1)"""
PROJECTION_TEST_NONFINITE_VALUES = (float("inf"), float("nan"))


def _verify_owned_projection_window(window, *, deferred=False, read_only=False):
    def reset_projection_window_fixture():
        owner = ledger_writer.current_ledger_writer_connection()
        owner.execute("DELETE FROM spend_events")
        owner.execute("DELETE FROM calls")
    calls._calls_db()
    budget._ledger()
    ledger_writer.run_ledger_write(reset_projection_window_fixture)
    current = budget._ledger()
    event = dict(PROJECTION_TEST_EVENT, id=window, chain=window)
    if window == "meta":
        event["is_meta"] = 1
    if window == "external":
        event.update(realtime_usd="0", external_usd=str(PROJECTION_TEST_AMOUNT))
    current.record_event(event)
    if window == "intent":
        connection = calls._calls_db()
        connection.execute(PROJECTION_TEST_CALL_SQL,
            (budget._utc().isoformat(timespec="seconds"), window,
             PROJECTION_TEST_AMOUNT, int(PROJECTION_TEST_AMOUNT * NANO_USD_PER_USD)))
        connection.commit()
    queries = {
        "realtime": lambda: budget.spent_since(PROJECTION_TEST_DAY,
            deadline_s=config.LEDGER_QUERY_DEADLINE_S, read_only=read_only),
        "job": lambda: budget.spent_by_job(window, deadline_s=config.LEDGER_QUERY_DEADLINE_S),
        "flow": lambda: budget.spent_all_time(deadline_s=config.LEDGER_QUERY_DEADLINE_S),
        "meta": budget.meta_spent_today,
        "external": budget.external_spent_today,
        "intent": lambda: calls.intent_spend(window),
    }
    def exercise_independent_projection_window():
        owner = ledger_writer.current_ledger_writer_connection()
        cached = budget._ledger()
        cached._conn = owner
        cached._explicit_owned_connection = True
        previous_cached = getattr(budget._LEDGER_TL, "led", None)
        budget._LEDGER_TL.led = cached
        category = "external_usd" if window == "external" else "realtime_usd"
        owner.execute(f"UPDATE spend_events SET {category}=?, {category}_nano=? WHERE id=?",
            (str(PROJECTION_TEST_CAP), int(PROJECTION_TEST_CAP * NANO_USD_PER_USD), window))
        if window == "intent":
            owner.execute("UPDATE calls SET cost=?,cost_nano=? WHERE intent=?",
                (PROJECTION_TEST_CAP, int(PROJECTION_TEST_CAP * NANO_USD_PER_USD), window))
        owner._ledger_bulk_commit_deferred = deferred
        try:
            with sqlite3.connect(config.db_path(), timeout=0) as connection:
                assert connection.execute(PROJECTION_TEST_CHECKPOINT_SQL).fetchone()[0] == 1
            result = queries[window]()
            assert math.isfinite(result)
            expected = PROJECTION_TEST_CAP if deferred else PROJECTION_TEST_AMOUNT
            assert result == expected, (window, result)
            if window == "realtime" and not deferred:
                with patch.object(config, "class_cap", return_value=PROJECTION_TEST_CAP), \
                        patch.object(config, "daily_cap", return_value=PROJECTION_TEST_CAP), \
                        patch.object(config, "monthly_cap", return_value=PROJECTION_TEST_CAP):
                    projection = budget.exceeded(PROJECTION_TEST_AMOUNT,
                        deadline_s=config.LEDGER_QUERY_DEADLINE_S)
                    assert projection is None, projection
        finally:
            budget._LEDGER_TL.led = previous_cached
            owner._ledger_bulk_commit_deferred = False
            owner.rollback()
    ledger_writer.run_ledger_write(exercise_independent_projection_window)


def test_budget_projection_realtime_owned_writer():
    _verify_owned_projection_window("realtime")


def test_budget_projection_explicit_snapshot_survives_cached_writer():
    _verify_owned_projection_window("realtime", read_only=True)


def test_budget_projection_job_owned_writer():
    _verify_owned_projection_window("job")


def test_budget_projection_flow_owned_writer():
    _verify_owned_projection_window("flow")


def test_budget_projection_meta_owned_writer():
    _verify_owned_projection_window("meta")


def test_budget_projection_external_owned_writer():
    _verify_owned_projection_window("external")


def test_budget_projection_intent_owned_writer():
    _verify_owned_projection_window("intent")


def test_budget_projection_deferred_bulk_preserves_pending_all_windows():
    for window in ("realtime", "job", "flow", "meta", "external", "intent"):
        _verify_owned_projection_window(window, deferred=True)


def test_budget_projection_category_reads_share_selected_connection():
    for query in (budget.meta_spent_today, budget.external_spent_today):
        with patch.object(ledger_read, "_ledger_read_connection",
                          wraps=ledger_read._ledger_read_connection) as selected:
            assert math.isfinite(query())
            assert selected.call_count == 1


def test_budget_projection_flow_memo_invalidates_external_commit():
    _verify_owned_projection_window("flow")
    before = budget.spent_all_time()
    with sqlite3.connect(config.db_path()) as connection:
        connection.execute("UPDATE spend_events SET realtime_usd=?,realtime_usd_nano=?",
            (str(PROJECTION_TEST_CAP), int(PROJECTION_TEST_CAP * NANO_USD_PER_USD)))
    assert budget.spent_all_time() - before == PROJECTION_TEST_CAP - PROJECTION_TEST_AMOUNT


def test_budget_projection_windows_ignore_plain_pending_writer():
    def reset_projection_gate_fixture():
        owner = ledger_writer.current_ledger_writer_connection()
        owner.execute("DELETE FROM spend_events")
        owner.execute("DELETE FROM calls")
    calls._calls_db()
    budget._ledger()
    ledger_writer.run_ledger_write(reset_projection_gate_fixture)
    current = budget._ledger()
    current.record_event(PROJECTION_TEST_EVENT)
    for category in ("meta", "external"):
        event = dict(PROJECTION_TEST_EVENT, id=f"{PROJECTION_TEST_INTENT}-{category}",
                     chain=category, realtime_usd=str(PROJECTION_TEST_AMOUNT if category == "meta" else 0),
                     external_usd=str(PROJECTION_TEST_AMOUNT if category == "external" else 0),
                     is_meta=int(category == "meta"))
        current.record_event(event)
    call_connection = calls._calls_db()
    call_connection.execute(PROJECTION_TEST_CALL_SQL,
        (budget._utc().isoformat(timespec="seconds"), PROJECTION_TEST_INTENT,
         PROJECTION_TEST_AMOUNT, int(PROJECTION_TEST_AMOUNT * NANO_USD_PER_USD)))
    call_connection.commit()
    def exercise_owned_projection_writer():
        owner = ledger_writer.current_ledger_writer_connection()
        owner.execute(PROJECTION_TEST_PENDING_SQL,
                      (str(PROJECTION_TEST_CAP), PROJECTION_TEST_INTENT))
        with sqlite3.connect(config.db_path(), timeout=0) as checkpoint_connection:
            checkpoint_result = checkpoint_connection.execute(PROJECTION_TEST_CHECKPOINT_SQL).fetchone()
        assert checkpoint_result[0] == 1, checkpoint_result
        with ExitStack() as checks:
            checks.enter_context(patch.object(config, "class_cap", return_value=PROJECTION_TEST_CAP))
            checks.enter_context(patch.object(config, "daily_cap", return_value=PROJECTION_TEST_CAP))
            checks.enter_context(patch.object(config, "monthly_cap", return_value=PROJECTION_TEST_CAP))
            checks.enter_context(patch.object(config, "meta_cap", return_value=PROJECTION_TEST_CAP))
            checks.enter_context(patch.object(config, "external_cap", return_value=PROJECTION_TEST_CAP))
            checks.enter_context(patch.object(config, "intent_cap", return_value=PROJECTION_TEST_CAP))
            checks.enter_context(patch.object(config, "budget_backend", return_value="sqlite"))
            checks.enter_context(patch.object(gate, "_emit"))
            assert budget.exceeded(PROJECTION_TEST_AMOUNT) is None
            for query in (lambda: budget.spent_since(PROJECTION_TEST_DAY),
                          lambda: budget.spent_by_job(PROJECTION_TEST_INTENT), budget.spent_all_time,
                          budget.meta_spent_today, budget.external_spent_today,
                          lambda: calls.intent_spend(PROJECTION_TEST_INTENT)):
                total = query()
                assert math.isfinite(total)
            assert budget.spent_since(PROJECTION_TEST_DAY) == PROJECTION_TEST_AMOUNT
            assert budget.spent_by_job(PROJECTION_TEST_INTENT) == PROJECTION_TEST_AMOUNT
            assert budget.exceeded(PROJECTION_TEST_AMOUNT) is None
            assert budget.exceeded(PROJECTION_TEST_CAP) is not None
            assert budget.meta_exceeded(PROJECTION_TEST_AMOUNT) is None
            assert budget.external_exceeded(PROJECTION_TEST_AMOUNT) is None
            assert budget.meta_exceeded(PROJECTION_TEST_CAP) is not None
            assert budget.external_exceeded(PROJECTION_TEST_CAP) is not None
            gate._budget_check(PROJECTION_TEST_AMOUNT, PROJECTION_TEST_INTENT,
                               PROJECTION_TEST_INTENT, "realtime")
            gate._intent_door_gate(PROJECTION_TEST_INTENT, PROJECTION_TEST_AMOUNT,
                                   PROJECTION_TEST_INTENT, PROJECTION_TEST_INTENT)
            for refusal in (
                lambda: gate._budget_check(PROJECTION_TEST_CAP, PROJECTION_TEST_INTENT,
                                           PROJECTION_TEST_INTENT, "realtime"),
                lambda: gate._intent_door_gate(PROJECTION_TEST_INTENT, PROJECTION_TEST_CAP,
                                               PROJECTION_TEST_INTENT, PROJECTION_TEST_INTENT)):
                try:
                    refusal()
                except gate.SpendGateRefused:
                    pass
                else:
                    raise AssertionError("a real exceeded cap must refuse")
            assert owner.in_transaction
        owner.rollback()
    ledger_writer.run_ledger_write(exercise_owned_projection_writer)


def test_budget_projection_real_read_failure_is_not_infinite_cap():
    with patch.object(budget, "spent_since", side_effect=ledger_read.LedgerQueryDeadline(
            config.LEDGER_QUERY_DEADLINE_S)):
        try:
            result = budget.exceeded(PROJECTION_TEST_AMOUNT)
        except ledger_read.LedgerQueryDeadline:
            return
        raise AssertionError(f"real read failure must propagate, never manufacture cap {result!r}")


def test_budget_projection_intent_failure_refuses():
    with patch.object(config, "intent_cap", return_value=PROJECTION_TEST_CAP), patch.object(
            calls, "intent_spend", side_effect=sqlite3.OperationalError(PROJECTION_TEST_INTENT)):
        try:
            gate._intent_door_gate(PROJECTION_TEST_INTENT, PROJECTION_TEST_AMOUNT, "test", "test")
        except gate.SpendGateRefused:
            return
        raise AssertionError("intent read failure must fail closed")


def test_budget_projection_all_windows_real_snapshot_failure():
    for query in (lambda: budget.spent_since(PROJECTION_TEST_DAY),
                  lambda: budget.spent_by_job(PROJECTION_TEST_INTENT), budget.spent_all_time,
                  budget.meta_spent_today, budget.external_spent_today,
                  lambda: calls.intent_spend(PROJECTION_TEST_INTENT)):
        with patch.object(ledger_read, "_ledger_read_connection",
                          side_effect=ledger_read.LedgerReadTimeout(config.LEDGER_QUERY_DEADLINE_S)):
            try:
                query()
            except ledger_read.LedgerQueryDeadline:
                pass
            else:
                raise AssertionError("a real snapshot failure must propagate")


def test_budget_projection_rejects_nonfinite_caps_and_arithmetic():
    for invalid in PROJECTION_TEST_NONFINITE_VALUES:
        for cap_name, check in (("daily_cap", budget.exceeded),
                                ("meta_cap", budget.meta_exceeded),
                                ("external_cap", budget.external_exceeded)):
            with patch.object(config, cap_name, return_value=invalid):
                try:
                    check(PROJECTION_TEST_AMOUNT)
                except ValueError:
                    pass
                else:
                    raise AssertionError("nonfinite configured caps must fail closed")


def test_budget_projection_realtime_real_failure():
    query = lambda: budget.spent_since(PROJECTION_TEST_DAY)
    with patch.object(ledger_read, "_ledger_read_connection",
                      side_effect=ledger_read.LedgerReadTimeout(config.LEDGER_QUERY_DEADLINE_S)):
        try:
            query()
        except ledger_read.LedgerQueryDeadline:
            return
        raise AssertionError("a real snapshot failure must propagate")


def test_budget_projection_job_real_failure():
    query = lambda: budget.spent_by_job(PROJECTION_TEST_INTENT)
    with patch.object(ledger_read, "_ledger_read_connection",
                      side_effect=ledger_read.LedgerReadTimeout(config.LEDGER_QUERY_DEADLINE_S)):
        try:
            query()
        except ledger_read.LedgerQueryDeadline:
            return
        raise AssertionError("a real snapshot failure must propagate")


def test_budget_projection_flow_real_failure():
    query = budget.spent_all_time
    with patch.object(ledger_read, "_ledger_read_connection",
                      side_effect=ledger_read.LedgerReadTimeout(config.LEDGER_QUERY_DEADLINE_S)):
        try:
            query()
        except ledger_read.LedgerQueryDeadline:
            return
        raise AssertionError("a real snapshot failure must propagate")


def test_budget_projection_meta_real_failure():
    query = budget.meta_spent_today
    with patch.object(ledger_read, "_ledger_read_connection",
                      side_effect=ledger_read.LedgerReadTimeout(config.LEDGER_QUERY_DEADLINE_S)):
        try:
            query()
        except ledger_read.LedgerQueryDeadline:
            return
        raise AssertionError("a real snapshot failure must propagate")


def test_budget_projection_external_real_failure():
    query = budget.external_spent_today
    with patch.object(ledger_read, "_ledger_read_connection",
                      side_effect=ledger_read.LedgerReadTimeout(config.LEDGER_QUERY_DEADLINE_S)):
        try:
            query()
        except ledger_read.LedgerQueryDeadline:
            return
        raise AssertionError("a real snapshot failure must propagate")


def test_budget_projection_intent_real_failure():
    query = lambda: calls.intent_spend(PROJECTION_TEST_INTENT)
    with patch.object(ledger_read, "_ledger_read_connection",
                      side_effect=ledger_read.LedgerReadTimeout(config.LEDGER_QUERY_DEADLINE_S)):
        try:
            query()
        except ledger_read.LedgerQueryDeadline:
            return
        raise AssertionError("a real snapshot failure must propagate")


if __name__ == "__main__":
    raise SystemExit(run_pytest_style(globals()))

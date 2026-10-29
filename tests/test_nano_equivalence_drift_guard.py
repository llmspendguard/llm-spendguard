"""Permanent offline Stage 6 guard; no reader cutover.

Read-only diagnostic: import this file and call nano_equivalence_self_check(conn).
It reopens conn's on-disk main database with mode=ro, and returns named gaps
and signed deltas. It never initializes a schema or opens a writer.
"""
import contextlib
import itertools
import sqlite3
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

import spendguard
spendguard.require()
import pytest

from spendguard import ledger, ledger_read, calls, guard, calibrate, effort_titration, measurement
from spendguard.money_scale import (
    assert_nano_total_matches, nano_usd_to_usd, real_usd_to_nano_usd, usd_to_nano_usd, NANO_USD_PER_USD,
)
from _pytest_fixture_bridge import run_pytest_style

DRIFT_PROOF_DATABASE = 'nano-equivalence.sqlite'
DRIFT_PROOF_STATUSES = ('draft', 'posted', 'void', 'reversed', 'reconciled', 'locked', None)
DRIFT_PROOF_FLAGS = ((0, 0, None), (1, 0, None), (0, 1, None),
                     (0, 0, 'reconstructed'), (None, None, None))
DRIFT_PROOF_AMOUNTS = ('1.234567891', '-0.25', '0', None, '0.000000521', '10000000.000000001')
DRIFT_PROOF_REAL_AMOUNTS = (-1.25, 0.0, None, 0.0000005213, 0.1, 0.2, 10000000.000000002)
DRIFT_PROOF_USD = 'USD'
DRIFT_PROOF_OTHER_CURRENCY = 'EUR'
DRIFT_PROOF_DAY = '2026-01-02'
DRIFT_PROOF_OTHER_DAY = '2026-01-01'
DRIFT_PROOF_SUBNANO = '0.0000005213'
DRIFT_PROOF_ZERO_TOLERANCE = '0'
DRIFT_PROOF_CENT = '0.01'
DRIFT_PROOF_BINARY_INTEGER_BITS = 53
DRIFT_PROOF_MUTATION = 1
DRIFT_PROOF_EMPTY_DATABASE = ':memory:'
DRIFT_PROOF_NONVOID = "COALESCE(status,'') NOT IN ('void')"
DRIFT_PROOF_NO_META = 'COALESCE(is_meta,0)=0'
DRIFT_PROOF_TEXT_SPECS = (
    ('spent_dec', ledger.LLM_USD_COLS, ledger.SpendLedger._COUNTABLE),
    ('remote_dec', ('remote_compute_usd',), ledger.SpendLedger._COUNTABLE),
    ('est_value_dec', ('est_chat_usd',), ledger.SpendLedger._LIVE_FILTER),
    ('subscription_dec', ('subscription_usd',), ledger.SpendLedger._LIVE_FILTER),
)
DRIFT_PROOF_REAL_SPECS = (
    ('recorded_call_total', 'calls', 'cost', '1=1'),
    ('incomplete_settlement_floor', 'calls', 'input_cost_floor', 'settlement_complete=0'),
    ('saved_since', 'savings', 'amount', '1=1'),
    ('decisions_summary', 'decisions', 'saved_usd', '1=1'),
    ('calibration_cost', 'calls', 'cost', ledger_read.CALIBRATION_CORPUS_PREDICATE),
    ('effort_checkpoint_cost', 'effort_titration', 'cost', '1=1'),
    ('measurement_spend_total', 'measurements', 'spend_usd', '1=1'),
)


def nano_equivalence_self_check(connection):
    """Report every canonical total; gaps/drift are explicit, never a clean pass.

    Requires a file-backed connection. An in-memory/uncommitted database cannot
    be checked as a committed read-only snapshot and is reported as a gap.
    All queries share one read transaction. NULL raw amounts are absent amounts;
    non-NULL raw values with NULL mirrors are named row gaps.
    """
    database_path = next((row[2] for row in connection.execute('PRAGMA database_list')
                          if row[1] == 'main'), '')
    if not database_path or connection.in_transaction:
        return {'snapshot': {'gaps': ['file_backed_committed_snapshot_required'], 'ok': False}}
    report = {}
    with contextlib.closing(sqlite3.connect(Path(database_path).as_uri() + '?mode=ro', uri=True)) as readonly:
        readonly.execute('BEGIN')
        readonly.create_aggregate('dec_sum', 1, ledger._DecSum)
        specs = [(name, 'spend_events', cols, filt, False)
                 for name, cols, filt in DRIFT_PROOF_TEXT_SPECS]
        for include_meta, include_void in itertools.product((False, True), repeat=2):
            filt = '1=1'
            if not include_meta:
                filt += ' AND ' + DRIFT_PROOF_NO_META
            if not include_void:
                filt += ' AND ' + DRIFT_PROOF_NONVOID
            specs.append((f'sum_dec:meta={include_meta}:void={include_void}',
                          'spend_events', ledger.USD_COLS, filt, False))
        specs.extend((name, table, (col,), filt, True)
                     for name, table, col, filt in DRIFT_PROOF_REAL_SPECS)
        groups = readonly.execute('SELECT DISTINCT day FROM spend_events').fetchall() if readonly.execute(
            "SELECT 1 FROM sqlite_master WHERE name='spend_events' AND type='table'").fetchone() else []
        for (day,) in groups:
            # Day values are parameters below; never interpolate database content.
            specs.append((f'sum_by:{day}', 'spend_events', ledger.USD_COLS,
                          ledger.SpendLedger._COUNTABLE, False))
        for name, table, columns, predicate, is_real in specs:
            available = {row[1] for row in readonly.execute(f'PRAGMA table_info({table})')}
            gaps = [f'missing_column:{table}.{col}' for col in columns
                    if col not in available]
            gaps += [f'missing_mirror:{table}.{col}_nano' for col in columns
                     if col + '_nano' not in available]
            required_filter_columns = (('settlement_complete',) if name == 'incomplete_settlement_floor'
                                       else ('in_tok', 'out_tok', 'model', 'intent') if name == 'calibration_cost'
                                       else ('status', 'is_meta', 'reconciled', 'cost_basis', 'currency', 'day')
                                       if table == 'spend_events' else ())
            gaps += [f'missing_filter_column:{table}.{col}' for col in required_filter_columns
                     if col not in available]
            entry = {'gaps': gaps, 'reader_result': None, 'nano_result': None,
                     'delta': None, 'ok': False}
            report[name] = entry
            if gaps:
                continue
            parameters = ()
            if name == 'calibration_cost':
                predicate += ' AND (intent IS NULL OR intent NOT LIKE ?)'
                parameters = (calibrate.META_PREFIX + '%',)
            if name.startswith('sum_by:'):
                predicate += ' AND day IS ?'
                parameters = (name.removeprefix('sum_by:'),)
                if parameters == ('None',):
                    parameters = (None,)
            for col in columns:
                for rowid, currency in readonly.execute(
                        f'SELECT rowid,{"currency" if table == "spend_events" else "NULL"} FROM {table} '
                        f'WHERE ({predicate}) AND {col} IS NOT NULL AND {col}_nano IS NULL', parameters):
                    gaps.append(f'missing_nano:{table}.{col}:rowid={rowid}:currency={currency}')
            aggregate = 'SUM' if is_real else 'dec_sum'
            raw_totals = readonly.execute(
                f'SELECT {",".join(f"{aggregate}({col})" for col in columns)} FROM {table} WHERE {predicate}',
                parameters).fetchone()
            raw_total = sum((Decimal(str(value or 0)) for value in raw_totals), Decimal(0))
            # Separate SUM per column avoids SQLite row-expression NULL arithmetic.
            nano_total = sum(value or 0 for value in readonly.execute(
                f'SELECT {",".join(f"SUM({col}_nano)" for col in columns)} FROM {table} WHERE {predicate}',
                parameters).fetchone())
            entry.update(reader_result=raw_total, nano_result=nano_usd_to_usd(nano_total),
                         delta=nano_usd_to_usd(nano_total) - raw_total)
            try:
                assert_nano_total_matches(nano_total, raw_total, label=name,
                    tolerance_usd=DRIFT_PROOF_CENT if is_real else DRIFT_PROOF_ZERO_TOLERANCE)
                if is_real:
                    converted = sum(real_usd_to_nano_usd(row[0]) for row in readonly.execute(
                        f'SELECT {columns[0]} FROM {table} WHERE ({predicate}) AND {columns[0]} IS NOT NULL', parameters))
                    assert_nano_total_matches(nano_total, nano_usd_to_usd(converted),
                                              tolerance_usd=DRIFT_PROOF_ZERO_TOLERANCE, label=name)
            except AssertionError as drift:
                entry['drift'] = str(drift)
            entry['ok'] = not gaps and 'drift' not in entry
            if name == 'decisions_summary' and readonly.execute(
                    'SELECT 1 FROM decisions WHERE saved_usd IS NULL').fetchone():
                entry.update(reader_result=None, delta=None, ok=False)
                gaps.append('unknown_reader_total:decisions.saved_usd:NULL')
    return report


def create_nano_equivalence_fixture(directory):
    path = directory / DRIFT_PROOF_DATABASE
    database = sqlite3.connect(path, check_same_thread=False)
    spend_ledger = ledger.SpendLedger(path, owned_connection=database)
    for index, (status, flags, amount) in enumerate(itertools.product(
            DRIFT_PROOF_STATUSES, DRIFT_PROOF_FLAGS, DRIFT_PROOF_AMOUNTS)):
        meta, reconciled, basis = flags
        columns = ('id', 'currency', 'status', 'is_meta', 'reconciled', 'cost_basis', 'day', *ledger.USD_COLS)
        database.execute(f'INSERT INTO spend_events ({",".join(columns)}) VALUES ({",".join("?" for _ in columns)})',
                         (str(index), DRIFT_PROOF_USD, status, meta, reconciled, basis,
                          DRIFT_PROOF_DAY if index % len(DRIFT_PROOF_FLAGS) else DRIFT_PROOF_OTHER_DAY,
                          *((amount,) * len(ledger.USD_COLS))))
    # Populate via the actual Stage 2 backfill, rather than a duplicate converter.
    for col in ledger.SPEND_EVENT_NANO_COLUMNS.values():
        database.execute(f'ALTER TABLE spend_events DROP COLUMN {col}')
    database.commit()
    spend_ledger._ensure_schema()
    calls._ensure_call_observation_schema(database)
    guard._ensure_guard_savings_schema(database)
    from spendguard.routing_audit import ensure_decisions_call_fk_schema
    ensure_decisions_call_fk_schema(database)
    effort_titration._ensure_effort_progress_schema(database)
    # Measurements normally have a separate DB; use its actual schema in this isolated fixture.
    with patch.object(measurement.config, 'HOME', directory):
        measurement_db = measurement._measurements_db()
        schema = measurement_db.execute("SELECT sql FROM sqlite_master WHERE name='measurements'").fetchone()[0]
        database.execute(schema)
        measurement_db.close()
    for index, amount in enumerate(DRIFT_PROOF_REAL_AMOUNTS):
        for complete in (0, 1, None):
            database.execute('INSERT INTO calls(id,cost,input_cost_floor,settlement_complete,in_tok,out_tok,intent,kind,ts,model) VALUES(?,?,?,?,?,?,?,?,?,?)',
                (f'{index}:{complete}', amount, amount, complete, 1, 1, __name__, 'batch', DRIFT_PROOF_DAY, __name__))
        database.execute('INSERT INTO savings(amount,source,day) VALUES(?,?,?)', (amount, __name__, DRIFT_PROOF_DAY))
        database.execute('INSERT INTO decisions(saved_usd,intent,day) VALUES(?,?,?)', (amount, __name__, DRIFT_PROOF_DAY))
        database.execute('INSERT INTO effort_titration(intent,model,effort,cost) VALUES(?,?,?,?)',
                         (str(index), __name__, __name__, amount))
        database.execute('INSERT INTO measurements(reading_id,spend_usd) VALUES(?,?)', (str(index), amount))
    # Run the actual backfills on pre-existing NULL mirrors.
    calls._backfill_call_observation_nano_columns(database, set(calls.CALL_OBSERVATION_NANO_COLUMNS.values()))
    guard._backfill_savings_nano_columns(database, set(guard.SAVINGS_NANO_COLUMNS.values()))
    from spendguard import routing_audit
    routing_audit._backfill_decision_nano_columns(database, set(routing_audit.DECISION_NANO_COLUMNS.values()))
    effort_titration._backfill_effort_progress_nano_columns(database, set(effort_titration.EFFORT_PROGRESS_NANO_COLUMNS.values()))
    measurement._backfill_measurement_nano_columns(database)
    database.commit()
    return path, database, spend_ledger


def test_nano_equivalence_canonical_readers_and_filter_cross_product(tmp_path):
    path, database, spend_ledger = create_nano_equivalence_fixture(tmp_path)
    try:
        report = nano_equivalence_self_check(database)
        assert report and all(entry['ok'] for name, entry in report.items()
                              if name != 'decisions_summary'), report
        assert report['decisions_summary']['gaps'] == ['unknown_reader_total:decisions.saved_usd:NULL']
        for name, _, _ in DRIFT_PROOF_TEXT_SPECS:
            assert report[name]['reader_result'] == Decimal(getattr(spend_ledger, name)())
        for meta, void in itertools.product((False, True), repeat=2):
            assert report[f'sum_dec:meta={meta}:void={void}']['reader_result'] == Decimal(
                spend_ledger.sum_dec(include_meta=meta, include_void=void))
        grouped = spend_ledger.sum_by('day', filt=ledger.SpendLedger._COUNTABLE)
        for day, value in grouped.items():
            assert report[f'sum_by:{day}']['nano_result'] == Decimal(value['usd'])
        # Scoped canonical readers must also agree, not only the whole fixture.
        for status in DRIFT_PROOF_STATUSES:
            if status is None:
                continue  # _where uses equality; NULL lifecycle is checked by the whole totals.
            for name, cols, filt in DRIFT_PROOF_TEXT_SPECS:
                scoped_total = sum(value or 0 for value in database.execute(
                    f'SELECT {",".join(f"SUM({col}_nano)" for col in cols)} FROM spend_events '
                    f'WHERE ({filt}) AND status=? AND day>=? AND day<=?',
                    (status, DRIFT_PROOF_DAY, DRIFT_PROOF_DAY)).fetchone())
                assert_nano_total_matches(scoped_total, Decimal(getattr(spend_ledger, name)(
                    since=DRIFT_PROOF_DAY, until=DRIFT_PROOF_DAY, where={'status': status})),
                    tolerance_usd=DRIFT_PROOF_ZERO_TOLERANCE, label=f'{name}:{status}')
        assert database.execute('SELECT SUM(batch_usd_nano) FROM spend_events').fetchone()[0] > (1 << DRIFT_PROOF_BINARY_INTEGER_BITS)
        floor = ledger_read._read_incomplete_settlement_totals(database)
        assert abs(report['incomplete_settlement_floor']['nano_result'] - Decimal(str(floor['input_cost_floor']))) <= Decimal(DRIFT_PROOF_CENT)
        with pytest.raises(ledger_read.LedgerSettlementIncomplete):
            ledger_read.read_recorded_call_total(path)
        database.execute('UPDATE calls SET settlement_complete=1,token_usage_complete=1')
        database.commit()
        assert_nano_total_matches(int(report['recorded_call_total']['nano_result'] * NANO_USD_PER_USD),
                                 Decimal(str(ledger_read.read_recorded_call_total(path))), label='actual recorded reader')
        with patch.object(guard.config, 'db_path', return_value=path):
            assert abs(report['saved_since']['nano_result'] - Decimal(str(guard.saved_since()['total']))) <= Decimal(DRIFT_PROOF_CENT)
            # NULL saved_usd is deliberately unknown in the unlimited reader.
            assert guard.decisions_summary()['saved_usd'] is None
            assert guard.decisions_summary(intent_limit=len(DRIFT_PROOF_REAL_AMOUNTS))['saved_usd'] is None
            database.execute('DELETE FROM decisions WHERE saved_usd IS NULL')
            database.commit()
            assert abs(report['decisions_summary']['nano_result'] - Decimal(str(guard.decisions_summary(intent_limit=len(DRIFT_PROOF_REAL_AMOUNTS))['saved_usd']))) <= Decimal(DRIFT_PROOF_CENT)
            assert abs(report['decisions_summary']['nano_result'] - Decimal(str(guard.decisions_summary()['saved_usd']))) <= Decimal(DRIFT_PROOF_CENT)
        with patch.object(ledger_read.config, 'db_path', return_value=path):
            calibration_total = sum(row[5] for row in calibrate._calls_rows(None, None, None))
            assert abs(report['calibration_cost']['nano_result'] - Decimal(str(calibration_total))) <= Decimal(DRIFT_PROOF_CENT)
        with patch.object(effort_titration.config, 'ledger_op', return_value=contextlib.nullcontext(database)):
            checkpoint_total = sum(effort_titration._load_progress(str(index), __name__, [__name__])[__name__]['cost']
                                   for index in range(len(DRIFT_PROOF_REAL_AMOUNTS)))
            assert abs(report['effort_checkpoint_cost']['nano_result'] - Decimal(str(checkpoint_total))) <= Decimal(DRIFT_PROOF_CENT)
        # Measurement readers use a distinct file; point the established read connection at this fixture.
        with patch.object(measurement, '_measurements_db', return_value=database):
            measured_total = sum(row['spend_usd'] or 0 for row in measurement.list_readings(limit=len(DRIFT_PROOF_REAL_AMOUNTS)))
            assert abs(report['measurement_spend_total']['nano_result'] - Decimal(str(measured_total))) <= Decimal(DRIFT_PROOF_CENT)
    finally:
        database.close()


def test_nano_equivalence_readonly_gaps_and_drift_are_never_passed(tmp_path):
    path, database, spend_ledger = create_nano_equivalence_fixture(tmp_path)
    try:
        before = path.read_bytes()
        changes = database.total_changes
        first = nano_equivalence_self_check(database)
        assert first == nano_equivalence_self_check(database)
        assert before == path.read_bytes() and changes == database.total_changes
        database.execute('UPDATE spend_events SET batch_usd_nano=batch_usd_nano+? WHERE rowid=(SELECT MIN(rowid) FROM spend_events)', (DRIFT_PROOF_MUTATION,))
        database.commit()
        assert not nano_equivalence_self_check(database)['spent_dec']['ok']
        database.execute('UPDATE savings SET amount_nano=amount_nano+? WHERE rowid=(SELECT MIN(rowid) FROM savings)',
                         (DRIFT_PROOF_MUTATION,))
        database.commit()
        real_drift = nano_equivalence_self_check(database)['saved_since']
        assert not real_drift['ok'] and 'drift' in real_drift
        database.execute('ALTER TABLE savings DROP COLUMN amount_nano')
        database.commit()
        assert 'missing_mirror:savings.amount_nano' in nano_equivalence_self_check(database)['saved_since']['gaps']
        database.execute('INSERT INTO spend_events(id,currency,status,batch_usd) VALUES(?,?,?,?)',
                         (__name__, DRIFT_PROOF_OTHER_CURRENCY, 'posted', DRIFT_PROOF_SUBNANO))
        # Real Stage 2 denomination handling leaves the foreign amount unconverted.
        spend_ledger._backfill_spend_event_nano_columns()
        database.commit()
        result = nano_equivalence_self_check(database)['spent_dec']
        assert not result['ok'] and any(DRIFT_PROOF_OTHER_CURRENCY in gap for gap in result['gaps'])
        assert database.execute('SELECT batch_usd_nano FROM spend_events WHERE id=?', (__name__,)).fetchone()[0] is None
        # Exclude the foreign-currency gap by denomination, not by guessing row meaning.
        foreign_total = database.execute('SELECT SUM(batch_usd_nano) FROM spend_events WHERE currency=?',
                                         (DRIFT_PROOF_OTHER_CURRENCY,)).fetchone()[0]
        assert foreign_total is None
        usd_total = sum(value or 0 for value in database.execute(
            'SELECT SUM(batch_usd_nano),SUM(realtime_usd_nano) FROM spend_events '
            f'WHERE currency=? AND ({ledger.SpendLedger._COUNTABLE})', (DRIFT_PROOF_USD,)).fetchone())
        assert_nano_total_matches(usd_total, Decimal(spend_ledger.spent_dec(where={'currency': DRIFT_PROOF_USD})),
                                  tolerance_usd=DRIFT_PROOF_ZERO_TOLERANCE, label='USD-only total with denomination gap')
        database.execute('DELETE FROM spend_events WHERE id=?', (__name__,))
        database.execute('INSERT INTO spend_events(id,currency,status,batch_usd,batch_usd_nano) VALUES(?,?,?,?,?)',
                         (__name__, DRIFT_PROOF_USD, 'posted', DRIFT_PROOF_SUBNANO, usd_to_nano_usd(DRIFT_PROOF_SUBNANO)))
        database.commit()
        subnano = nano_equivalence_self_check(database)['spent_dec']
        assert not subnano['ok'] and subnano['delta'] == nano_usd_to_usd(
            usd_to_nano_usd(DRIFT_PROOF_SUBNANO)) - Decimal(DRIFT_PROOF_SUBNANO)
        with sqlite3.connect(DRIFT_PROOF_EMPTY_DATABASE) as memory:
            assert nano_equivalence_self_check(memory)['snapshot']['ok'] is False
        # A genuinely unmigrated database is reported without executing an initializer.
        database.execute('ALTER TABLE calls DROP COLUMN cost_nano')
        database.commit()
        assert 'missing_mirror:calls.cost_nano' in nano_equivalence_self_check(database)['recorded_call_total']['gaps']
    finally:
        database.close()


if __name__ == '__main__':
    raise SystemExit(run_pytest_style(globals()))

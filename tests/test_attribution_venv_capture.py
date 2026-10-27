"""Offline guards for required spend-event attribution captured from the running venv."""
import os
import sqlite3
import sys
import tempfile

if not os.environ.get("SPENDGUARD_TEST_ISOLATED"):
    os.environ["SPENDGUARD_TEST_ISOLATED"] = "1"
    os.environ["SPENDGUARD_NO_AUTOINSTALL"] = "1"
    os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="spendguard-attribution-venv-")
    os.execv(sys.executable, [sys.executable] + sys.argv)

from spendguard import budget, ledger, config


def check_attribution_venv_capture_condition(name, condition):
    print(("  [OK] " if condition else "  [FAIL] ") + name)
    return 0 if condition else 1


TEST_PROVIDER = "attribution-fixture-provider"
TEST_MODEL = "attribution-fixture-model"
TEST_COST = 0.01
UNATTRIBUTED_EXPECTED = "unattributed"
PARAM_WRITTEN_VENV = "/workspace/medical-app/.venv.nosync"
PARAM_WRITTEN_SOURCE = "attribution-param-venv"
OMITTED_VENV_SOURCE = "attribution-omitted-venv"

failures = 0

# Build an existing ledger from the real DDL with the new column removed, then prove opening it migrates in place.
migration_path = os.path.join(os.environ["SPENDGUARD_HOME"], "pre-venv-column.db")
legacy_ddl = "\n".join(line for line in ledger._DDL_EVENTS.splitlines()
                       if not line.strip().startswith("venv "))
migration_connection = sqlite3.connect(migration_path)
migration_connection.executescript(legacy_ddl)
migration_connection.commit()
migration_connection.close()
migrated_ledger = ledger.SpendLedger(db_path=migration_path, owned_connection=config.open_ledger_connection(migration_path))
migrated_columns = {row[1] for row in migrated_ledger._conn.execute("PRAGMA table_info(spend_events)")}
failures += check_attribution_venv_capture_condition(
    "existing ledger gains the additive venv column on open", "venv" in migrated_columns)

budget.record_charge(TEST_PROVIDER, TEST_MODEL, "realtime", TEST_COST, project="")
active_ledger = budget._ledger()
active_ledger._restore_spend_aggregate_connection()
active_ledger._explicit_owned_connection = True
gate_row = active_ledger._conn.execute(
    "SELECT venv, project_primary FROM spend_events WHERE source=? AND model=? ORDER BY rowid DESC LIMIT 1",
    ("gate", TEST_MODEL),
).fetchone()
failures += check_attribution_venv_capture_condition(
    "gate row captures the running interpreter venv", gate_row["venv"] == sys.prefix)

budget._record_spend_event(
    TEST_PROVIDER, TEST_MODEL, "batch", TEST_COST, project="", source="reconstructed")
non_gate_row = active_ledger._conn.execute(
    "SELECT venv, project_primary FROM spend_events WHERE source=? AND model=? ORDER BY rowid DESC LIMIT 1",
    ("reconstructed", TEST_MODEL),
).fetchone()
failures += check_attribution_venv_capture_condition(
    "non-gate row leaves venv absent", non_gate_row["venv"] in (None, ""))

param_written_event_id = active_ledger.record_event(
    {"provider": TEST_PROVIDER, "model": TEST_MODEL, "kind": "realtime", "usd": TEST_COST,
     "source": PARAM_WRITTEN_SOURCE},
    venv=PARAM_WRITTEN_VENV,
)
param_written_row = active_ledger._conn.execute(
    "SELECT venv FROM spend_events WHERE id=?", (param_written_event_id,)
).fetchone()
failures += check_attribution_venv_capture_condition(
    "record_event venv parameter writes the venv column", param_written_row["venv"] == PARAM_WRITTEN_VENV)

omitted_venv_event_id = active_ledger.record_event(
    {"provider": TEST_PROVIDER, "model": TEST_MODEL, "kind": "realtime", "usd": TEST_COST,
     "source": OMITTED_VENV_SOURCE}
)
omitted_venv_row = active_ledger._conn.execute(
    "SELECT venv FROM spend_events WHERE id=?", (omitted_venv_event_id,)
).fetchone()
failures += check_attribution_venv_capture_condition(
    "record_event without venv leaves the venv column absent", omitted_venv_row["venv"] in (None, ""))

failures += check_attribution_venv_capture_condition(
    "gate row keeps its captured venv while using the visible sentinel",
    bool(gate_row["venv"])
    and gate_row["project_primary"] == UNATTRIBUTED_EXPECTED)
failures += check_attribution_venv_capture_condition(
    "row without explicit project or venv uses the visible sentinel",
    non_gate_row["project_primary"] == UNATTRIBUTED_EXPECTED)

captured_paths = active_ledger.captured_spend_source_paths()
failures += check_attribution_venv_capture_condition(
    "captured spend source paths include the running venv", sys.prefix in captured_paths)
failures += check_attribution_venv_capture_condition(
    "captured spend source paths include the parameter-written venv", PARAM_WRITTEN_VENV in captured_paths)

print(("[OK]" if not failures else "[FAIL]")
      + " attribution venv capture: %d failure(s)" % failures)
sys.exit(1 if failures else 0)

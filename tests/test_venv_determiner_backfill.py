"""Offline checks for project backfill from captured virtual environments."""
import os
import sys
import tempfile

if not os.environ.get("SPENDGUARD_TEST_ISOLATED"):
    os.environ["SPENDGUARD_TEST_ISOLATED"] = "1"
    os.environ["SPENDGUARD_NO_AUTOINSTALL"] = "1"
    os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="spendguard-venv-determiner-")
    os.execv(sys.executable, [sys.executable] + sys.argv)

from spendguard import budget, tag


def check_venv_determiner_backfill_condition(name, condition):
    print(("  [OK] " if condition else "  [FAIL] ") + name)
    return 0 if condition else 1


TEST_PROVIDER = "venv-determiner-provider"
TEST_COST = 0.01
INVENTORY_MODEL = "inventory-app-row"
CATALOG_MODEL = "catalog-service-row"
AMBIGUOUS_MODEL = "ambiguous-venv-row"
NO_VENV_MODEL = "no-venv-row"
INVENTORY_VENV = "/workspace/inventory-app/.venv"
CATALOG_VENV = "/workspace/catalog-service/.venv"
AMBIGUOUS_VENV = "/opt/shared/.venv-tools"

for model in (INVENTORY_MODEL, CATALOG_MODEL, AMBIGUOUS_MODEL, NO_VENV_MODEL):
    budget._record_spend_event(
        TEST_PROVIDER, model, "batch", TEST_COST, project="", source="reconstructed")

active_ledger = budget._ledger()
active_ledger._conn.execute(
    "UPDATE spend_events SET venv=? WHERE model=?", (INVENTORY_VENV, INVENTORY_MODEL))
active_ledger._conn.execute(
    "UPDATE spend_events SET venv=? WHERE model=?", (CATALOG_VENV, CATALOG_MODEL))
active_ledger._conn.execute(
    "UPDATE spend_events SET venv=? WHERE model=?", (AMBIGUOUS_VENV, AMBIGUOUS_MODEL))
active_ledger._conn.commit()

retagged = tag.backfill_project_from_venv(active_ledger)
projects_by_model = {
    row["model"]: row["project_primary"]
    for row in active_ledger._conn.execute(
        "SELECT model, project_primary FROM spend_events WHERE provider=?", (TEST_PROVIDER,))
}

failures = 0
failures += check_venv_determiner_backfill_condition(
    "inventory-app venv resolves to inventory-app",
    projects_by_model[INVENTORY_MODEL] == "inventory-app")
failures += check_venv_determiner_backfill_condition(
    "catalog-service repo-local venv resolves to catalog-service",
    projects_by_model[CATALOG_MODEL] == "catalog-service")
failures += check_venv_determiner_backfill_condition(
    "ambiguous venv remains unresolved for the agentic or coarse path",
    projects_by_model[AMBIGUOUS_MODEL] == budget.UNATTRIBUTED_PROJECT)
failures += check_venv_determiner_backfill_condition(
    "row without a venv remains unresolved",
    projects_by_model[NO_VENV_MODEL] == budget.UNATTRIBUTED_PROJECT)
failures += check_venv_determiner_backfill_condition(
    "backfill reports exactly two retagged rows", retagged == 2)

print(("[OK]" if not failures else "[FAIL]")
      + " venv determiner backfill: %d failure(s)" % failures)
sys.exit(1 if failures else 0)

"""Offline checks for deterministic retag ordering and unresolved-sentinel handling."""
import os
import sys
import tempfile
import sqlite3
from contextlib import closing

if not os.environ.get("SPENDGUARD_TEST_ISOLATED"):
    isolated_home = tempfile.mkdtemp(prefix="spendguard-retag-meta-precedence-")
    os.environ["SPENDGUARD_TEST_ISOLATED"] = "1"
    os.environ["SPENDGUARD_NO_AUTOINSTALL"] = "1"
    os.environ["SPENDGUARD_HOME"] = isolated_home
    os.environ["HOME"] = isolated_home
    os.execv(sys.executable, [sys.executable] + sys.argv)

from spendguard import budget, tag


def check_retag_meta_precedence_condition(name, condition):
    print(("  [OK] " if condition else "  [FAIL] ") + name)
    return 0 if condition else 1


TEST_PROVIDER = "retag-meta-precedence-provider"
TEST_COST = 0.01
META_MODEL = "meta-inside-consumer-venv"
CONSUMER_MODEL = "consumer-venv-row"
SHARED_MODEL = "shared-venv-row"
NO_VENV_MODEL = "no-venv-row"
CONSUMER_VENV = "/workspace/medical-app/.venv"
SHARED_VENV = "/opt/shared/.venv-tools"

for model, kind in (
        (META_MODEL, "meta"),
        (CONSUMER_MODEL, "batch"),
        (SHARED_MODEL, "batch"),
        (NO_VENV_MODEL, "batch")):
    budget._record_spend_event(
        TEST_PROVIDER, model, kind, TEST_COST, project="", source="reconstructed")

from spendguard import config

with closing(config.open_ledger_connection()) as retag_fixture_connection:
    retag_fixture_connection.row_factory = sqlite3.Row
    active_ledger = budget._ledger()
    retag_fixture_connection.execute(
        "UPDATE spend_events SET venv=? WHERE model=?", (CONSUMER_VENV, META_MODEL))
    retag_fixture_connection.execute(
        "UPDATE spend_events SET venv=? WHERE model=?", (CONSUMER_VENV, CONSUMER_MODEL))
    retag_fixture_connection.execute(
        "UPDATE spend_events SET venv=? WHERE model=?", (SHARED_VENV, SHARED_MODEL))
    retag_fixture_connection.commit()

    tag.retag_deterministic()
    projects_by_model = {
        row["model"]: row["project_primary"]
        for row in retag_fixture_connection.execute(
            "SELECT model, project_primary FROM spend_events WHERE provider=?", (TEST_PROVIDER,))
    }

    failures = 0
    failures += check_retag_meta_precedence_condition(
        "A meta row keeps llm-spendguard precedence over its consumer venv",
        projects_by_model[META_MODEL] == "llm-spendguard")
    failures += check_retag_meta_precedence_condition(
        "B consumer gate row resolves from its medical-app venv",
        projects_by_model[CONSUMER_MODEL] == "medical-app")
    failures += check_retag_meta_precedence_condition(
        "C ambiguous venv falls through to the coarse repo fallback (not mechanically stamped)",
        projects_by_model[SHARED_MODEL] == budget._project())
    failures += check_retag_meta_precedence_condition(
        "C ambiguous venv is not stranded at the unresolved sentinel",
        projects_by_model[SHARED_MODEL] != budget.UNATTRIBUTED_PROJECT)
    failures += check_retag_meta_precedence_condition(
        "D row without a venv reaches the coarse repo fallback",
        projects_by_model[NO_VENV_MODEL] == budget._project())
    failures += check_retag_meta_precedence_condition(
        "D row is not stranded at the unresolved sentinel",
        projects_by_model[NO_VENV_MODEL] != budget.UNATTRIBUTED_PROJECT)

    print(("[OK]" if not failures else "[FAIL]")
          + " deterministic retag meta precedence: %d failure(s)" % failures)
    sys.exit(1 if failures else 0)

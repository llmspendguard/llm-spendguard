"""Offline concurrent governance faults; exact source mutations must turn each guard red."""
import concurrent.futures
import inspect
import os
import pathlib
import sqlite3
import subprocess
import sys
import tempfile
from unittest.mock import patch

import pytest

from spendguard import adapters, dispatch, xp_rate_window

GOVERNANCE_CHAOS_WORKERS = 4
GOVERNANCE_CHAOS_CALLERS = 12
GOVERNANCE_CHAOS_DEADLINE_S = 1
GOVERNANCE_CHAOS_RPM = 60
GOVERNANCE_CHAOS_VENDOR = "governance-chaos"
GOVERNANCE_CHAOS_MODEL = "governance-chaos-model"
GOVERNANCE_CHAOS_REASON = "broken governance store: " + "full failure detail; " * 12 + "tail evidence"
GOVERNANCE_CHAOS_MUTATIONS = ("intact", "dispatch_admit_open", "sqlite_reserve_open")
GOVERNANCE_CHAOS_CHILD_FLAG = "SPENDGUARD_GOVERNANCE_CHAOS_CHILD"
GOVERNANCE_CHAOS_PROCESS_BUDGET_S = 60


def install_governance_admit_mutation(mutation):
    """Replace only the fixed production arm, preserving the rest of each complete function."""
    if mutation == "dispatch_admit_open":
        target = dispatch.admit
        closed = ('return _Admission(False, False, False, vendor, model, False,\n'
                  '                          error=f"dispatch.admit: governor unavailable for {vendor}/{model} "\n'
                  '                                f"({type(_ge).__name__}: {_ge})")')
        opened = "return _Admission(True, False, False, vendor, model, False)"
    elif mutation == "sqlite_reserve_open":
        target = xp_rate_window.reserve
        closed = ('raise RateDeadline(f"xp_rate_window.reserve: \'{key}\' shared rate store failed "\n'
                  '                               f"({type(error).__name__}: {error})") from error')
        opened = "return clock() - t0"
    else:
        assert mutation == "intact", mutation
        return
    source = inspect.getsource(target)
    assert source.count(closed) == 1, "production mutation target drifted"
    exec(compile(source.replace(closed, opened), inspect.getsourcefile(target), "exec"), target.__globals__)


def exercise_governance_fault_contract(mutation):
    install_governance_admit_mutation(mutation)
    # Scope fake lane readiness to this wave: no real CLI discovery, no metered transport, no API keys.
    with patch.object(adapters, "_lane_for", return_value=None), \
            patch.object(dispatch, "_off", return_value=False), \
            patch.object(xp_rate_window, "_xp_rate_off", return_value=False):
        if mutation != "sqlite_reserve_open":
            with patch.object(dispatch.Governor, "_global_sem", side_effect=OSError(GOVERNANCE_CHAOS_REASON)):
                with concurrent.futures.ThreadPoolExecutor(max_workers=GOVERNANCE_CHAOS_WORKERS) as pool:
                    admissions = list(pool.map(lambda _: dispatch.admit(
                        GOVERNANCE_CHAOS_VENDOR, GOVERNANCE_CHAOS_MODEL, GOVERNANCE_CHAOS_DEADLINE_S),
                        range(GOVERNANCE_CHAOS_CALLERS)))
            for admission in admissions:
                assert not admission.ok, "dispatcher infrastructure failure admitted ungoverned work"
                assert not admission.held and not admission.shed
                assert "dispatch.admit" in admission.error and GOVERNANCE_CHAOS_REASON in admission.error

        # The real Governor path must turn the broken shared store into a refused admission and release slots.
        governor = dispatch.Governor()
        with patch.object(dispatch, "_GOV", governor), \
                patch.object(dispatch, "_catalog_rate", return_value=(GOVERNANCE_CHAOS_RPM, 0)), \
                patch.object(dispatch, "_learned_conn", return_value=0), \
                patch.object(dispatch._LearnedLimits, "for_vendor", return_value={}), \
                patch.object(xp_rate_window.config, "ledger_op", side_effect=sqlite3.DatabaseError(
                    GOVERNANCE_CHAOS_REASON)):
            with concurrent.futures.ThreadPoolExecutor(max_workers=GOVERNANCE_CHAOS_WORKERS) as pool:
                admissions = list(pool.map(lambda _: dispatch.admit(
                    GOVERNANCE_CHAOS_VENDOR, GOVERNANCE_CHAOS_MODEL, GOVERNANCE_CHAOS_DEADLINE_S,
                    shed=False), range(GOVERNANCE_CHAOS_CALLERS)))
        for admission in admissions:
            assert not admission.ok, "rate-window SQLite failure admitted ungoverned work"
            assert not admission.held and not admission.shed
            assert "xp_rate_window.reserve" in admission.error and GOVERNANCE_CHAOS_REASON in admission.error
        assert all(bucket.in_flight == 0 for bucket in governor._buckets.values()), "refusal leaked slots"
    print("GREEN governance fault contract: concurrent infrastructure refusal with complete reasons")


@pytest.mark.chaos
def test_governance_fail_closed_mutation_matrix():
    for mutation in GOVERNANCE_CHAOS_MUTATIONS:
        with tempfile.TemporaryDirectory() as scenario_home:
            env = dict(os.environ, SPENDGUARD_HOME=scenario_home, SPENDGUARD_TEST_ISOLATED="1",
                       SPENDGUARD_CHAOS_MUTATION=mutation)
            env[GOVERNANCE_CHAOS_CHILD_FLAG] = "1"
            proc = subprocess.run([sys.executable, str(pathlib.Path(__file__).resolve())], env=env,
                                  text=True, capture_output=True, timeout=GOVERNANCE_CHAOS_PROCESS_BUDGET_S)
        state = "GREEN" if proc.returncode == 0 else "RED"
        print(f"{mutation:26} {state}", flush=True)
        if mutation == "intact":
            assert proc.returncode == 0, proc.stdout + proc.stderr
        else:
            assertion = ("dispatcher infrastructure failure admitted ungoverned work"
                         if mutation == "dispatch_admit_open"
                         else "rate-window SQLite failure admitted ungoverned work")
            assert proc.returncode != 0 and assertion in proc.stderr, proc.stdout + proc.stderr


if __name__ == "__main__":
    if os.environ.get(GOVERNANCE_CHAOS_CHILD_FLAG):
        exercise_governance_fault_contract(os.environ["SPENDGUARD_CHAOS_MUTATION"])
    else:
        test_governance_fail_closed_mutation_matrix()

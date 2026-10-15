"""Runnable mutation contract: each removed production rung must make the same chaos contract red."""
import os
import pathlib
import subprocess
import sys


def lane_mutation_matrix():
    script = pathlib.Path(__file__).with_name("lane_faults.py")
    matrix = []
    for mutation in ("intact", "disable_retry", "disable_shed", "disable_queue", "allow_ungated_metered"):
        env = dict(os.environ, SPENDGUARD_CHAOS_MUTATION=mutation)
        proc = subprocess.run([sys.executable, str(script)], env=env, text=True, capture_output=True, timeout=60)
        state = "GREEN" if proc.returncode == 0 else "RED"
        print(f"{mutation:26} {state}", flush=True)
        matrix.append((mutation, state))
        if (proc.returncode == 0) != (mutation == "intact"):
            raise AssertionError(f"mutation not detected: {mutation}\n{proc.stdout}\n{proc.stderr}")
    return matrix


def work_mutation_matrix():
    matrix = []
    for contract, mutations in (
        ("exactly_once_faults", ("intact", "disable_idempotency", "drop_persisted_handle",
                                 "disable_message_idempotency", "drop_message_pending_record",
                                 "disable_terminal_manifest_assertion", "disable_global_ordinals")),
        ("never_lose_work_faults", ("intact", "swallow_persist_failure", "rebuy_expired_hold",
                                    "disable_disposition_assertion")),
    ):
        for mutation in mutations:
            env = dict(os.environ, SPENDGUARD_CHAOS_MUTATION=mutation)
            script = pathlib.Path(__file__).with_name(contract + ".py")
            proc = subprocess.run([sys.executable, str(script)], env=env, text=True, capture_output=True, timeout=60)
            state = "GREEN" if proc.returncode == 0 else "RED"
            print(f"{contract:26} {mutation:26} {state}", flush=True)
            matrix.append((contract, mutation, state))
            if (proc.returncode == 0) != (mutation == "intact"):
                raise AssertionError(f"mutation not detected: {contract}/{mutation}\n{proc.stdout}\n{proc.stderr}")
    return matrix


if __name__ == "__main__":
    lane_mutation_matrix()
    work_mutation_matrix()

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


if __name__ == "__main__":
    lane_mutation_matrix()

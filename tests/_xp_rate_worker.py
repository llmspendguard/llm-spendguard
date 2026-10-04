"""Subprocess worker for test_xp_rate_window_multiprocess.py (NOT a test itself — underscore-prefixed so the runner's
tests/test_*.py glob skips it). Fans M reserve() calls against ONE shared key under a per-second cap, writing the
wall-clock admission time of each to an output file so the parent can reconstruct the GLOBAL (cross-process) timeline
and prove aggregate rate <= cap. SPENDGUARD_HOME is inherited from the parent so all workers share one rate db.
"""
import json
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))
import spendguard  # noqa: E402
spendguard.require = lambda: None
from spendguard import xp_rate_window as X  # noqa: E402


def main():
    key, m, cap, out = sys.argv[1], int(sys.argv[2]), int(sys.argv[3]), sys.argv[4]
    limits = [(1.0, cap, None)]                          # a per-second cap: the axis that bounds the stampede
    stamps = []
    for _ in range(m):
        X.reserve(key, limits, deadline_s=120.0)
        stamps.append(time.time())                       # ~admission time (reserve recorded it <few ms earlier)
    with open(out, "w") as fh:
        json.dump(stamps, fh)


if __name__ == "__main__":
    main()

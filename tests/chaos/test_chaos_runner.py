"""Separate chaos tier, collected by the default CI pytest gate as well as -m chaos."""
import pathlib
import runpy

import pytest


@pytest.mark.chaos
def test_lane_ladder_mutation_contract():
    harness = runpy.run_path(str(pathlib.Path(__file__).with_name("mutation_proof.py")))
    harness["lane_mutation_matrix"]()

"""Gate queries release scarce read slots before downstream model-fact reads."""
import sys
from unittest.mock import patch

from _pytest_fixture_bridge import align_test_startup_home, run_pytest_style

align_test_startup_home(prefix=__name__)

from spendguard import bulkgate, config, models

GATE_NESTING_READ_CAP = 1
GATE_NESTING_WAIT_S = 0.01
GATE_NESTING_MODEL = __name__
GATE_NESTING_OUTPUT_TOKENS = 16


def test_maxtokens_releases_gate_lease_before_real_model_facts_read():
    # A learned fact forces the real models -> learn read path, including the
    # broad fallback in maxtokens that previously hid pool exhaustion as zero.
    models.add_fact(GATE_NESTING_MODEL, "reasoning", True, source=__name__)
    with patch.object(config, "ledger_read_pool_settings",
                      return_value=(GATE_NESTING_READ_CAP, GATE_NESTING_WAIT_S)):
        result = bulkgate.maxtokens(__name__, model=GATE_NESTING_MODEL)
    assert result["recommend"] == bulkgate._reasoning_out_estimate()


def test_model_outputs_releases_query_lease_before_distribution_work():
    bulkgate.note_response(__name__, GATE_NESTING_MODEL, GATE_NESTING_OUTPUT_TOKENS)
    original_percentile = bulkgate._pctl

    def read_model_facts_during_distribution(values, percentile):
        models.facts(GATE_NESTING_MODEL)
        return original_percentile(values, percentile)

    with patch.object(config, "ledger_read_pool_settings",
                      return_value=(GATE_NESTING_READ_CAP, GATE_NESTING_WAIT_S)), patch.object(
                          bulkgate, "_pctl", side_effect=read_model_facts_during_distribution):
        distribution = bulkgate.model_outputs(GATE_NESTING_MODEL)
    assert distribution["n_classes"] == GATE_NESTING_READ_CAP
    assert distribution["p99"] == GATE_NESTING_OUTPUT_TOKENS


if __name__ == "__main__":
    sys.exit(run_pytest_style(globals()))

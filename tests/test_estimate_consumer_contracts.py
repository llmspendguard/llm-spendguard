"""Offline producer/consumer regressions for persisted calibration and external scans."""
import contextlib
import io
import json
import pathlib
import sqlite3
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from spendguard import adapters, calibrate, config, estimate_divergence, estimate_literals, ledger_read

ESTIMATE_CONTRACT_ENTITY = "estimate-contract-fixture"
ESTIMATE_CONTRACT_AMOUNT = 0.25
ESTIMATE_CONTRACT_TOKENS = 24
ESTIMATE_CONTRACT_SOURCE = "import pricing\npricing.realtime_cost(model, 17, 29)\n"
ESTIMATE_CONTRACT_DATABASE_NAME = "calibration.sqlite"


class EstimateConsumerContractChecks(unittest.TestCase):
    def test_divergence_consumes_real_calibration_summary_and_persisted_pair(self):
        with tempfile.TemporaryDirectory() as temporary, sqlite3.connect(
                pathlib.Path(temporary) / ESTIMATE_CONTRACT_DATABASE_NAME) as connection:
            calibrate._ensure_cost_predictions_schema(connection)
            actual = SimpleNamespace(values=(ESTIMATE_CONTRACT_TOKENS, ESTIMATE_CONTRACT_TOKENS,
                                            ESTIMATE_CONTRACT_AMOUNT, "batch", ESTIMATE_CONTRACT_ENTITY))
            with (patch.object(calibrate, "_con", return_value=connection),
                  patch.object(calibrate, "current_ledger_writer_connection", return_value=connection),
                  patch.object(calibrate, "_identity", return_value=(None, None)),
                  patch.object(ledger_read, "read_calibration_actuals", return_value=[actual])):
                calibrate.record_prediction.__wrapped__(ESTIMATE_CONTRACT_ENTITY, ESTIMATE_CONTRACT_ENTITY,
                                                       ESTIMATE_CONTRACT_ENTITY, ESTIMATE_CONTRACT_AMOUNT)
                # Exercise the real producer, retaining its actual summary contract.
                with (patch.object(calibrate, "pair", wraps=calibrate.pair.__wrapped__) as pairing,
                      patch.object(config, "fresh_ledger_conn", side_effect=lambda initializer: sqlite3.connect(
                          pathlib.Path(temporary) / ESTIMATE_CONTRACT_DATABASE_NAME)),
                      patch.object(adapters, "call", return_value={"text": json.dumps({"grounded": True})}) as judge):
                    result = estimate_divergence.enforce(model=ESTIMATE_CONTRACT_ENTITY)
                self.assertEqual(pairing.call_count, 1)
                self.assertEqual(len(result["judged"]), 1)
                pair = result["judged"][0]["pair"]
                self.assertEqual(pair["job_id"], ESTIMATE_CONTRACT_ENTITY)
                self.assertEqual(pair["actual_usd"], ESTIMATE_CONTRACT_AMOUNT)
                self.assertIn('"predicted_usd"', judge.call_args.args[1])
                self.assertIn('"actual_usd"', judge.call_args.args[1])

    def test_literal_review_scans_supplied_root_and_defaults_to_working_directory(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = pathlib.Path(temporary)
            source = root / "src"
            source.mkdir()
            (source / "external_quote.py").write_text(ESTIMATE_CONTRACT_SOURCE)
            with contextlib.redirect_stdout(io.StringIO()) as output:
                self.assertEqual(estimate_literals.review_literal_token_estimates(repo_root=root), 1)
            self.assertIn("src/external_quote.py", output.getvalue())
            with patch.object(pathlib.Path, "cwd", return_value=root):
                with contextlib.redirect_stdout(io.StringIO()) as output:
                    self.assertEqual(estimate_literals.review_literal_token_estimates(), 1)
            self.assertIn("src/external_quote.py", output.getvalue())


if __name__ == "__main__":
    unittest.main()

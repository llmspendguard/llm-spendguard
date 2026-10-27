"""Null telemetry remains unknown across counterfactual and prompt readers."""
from types import SimpleNamespace
from unittest.mock import patch
import sqlite3

from spendguard import adapters, backfill, callio, conv, guard, lane_catalog, ledger_read, prompts, signal


def test_unknown_valuation_readers_preserve_missing_counts():
    with patch.object(lane_catalog, "parse_use_name", return_value=("model", None)), \
            patch.object(lane_catalog, "lane_provider", return_value="provider"):
        assert lane_catalog.use_name_cost("model", 1, None, "lane") is None
    with patch.object(guard, "record_decision") as unknown_decision:
        adapters._book_substitution({"substituted_from": "model", "model": "chosen", "in_tok": 1,
                                    "out_tok": None, "cost": 0})
        assert unknown_decision.call_args.kwargs["counterfactual_usd"] is None
    with patch.object(ledger_read, "read_prompt_lint_evidence", return_value=[
            SimpleNamespace(values=("intent", "model", 1, None, 0, "length", "", "subscription"))]):
        unknown_findings = prompts.lint(min_calls=1)
        assert any(finding["kind"] == "unknown_usage" and finding["est_usd"] is None
                   for finding in unknown_findings)
    with sqlite3.connect(":memory:") as signal_database:
        signal_database.execute("CREATE TABLE call_io(intent,model,batch,quality,in_tok,out_tok,ts)")
        signal_database.executemany("INSERT INTO call_io VALUES(?,?,?,?,?,?,?)", [
            ("intent", "model", "batch", None, 1, None, "day"),
            ("intent", "model", "batch", None, 1, 2, "day")])
        with patch.object(callio, "_callio_db", return_value=signal_database), \
                patch.object(backfill, "_openai_rows", return_value=[]), \
                patch.object(backfill, "_anthropic_rows", return_value=[]), \
                patch.object(conv, "batch_project_map", return_value={}), \
                patch.object(signal, "_cancelled_billed", return_value={}):
            signal_row, = signal.build_signals(since="")
            assert signal_row["tokens_out"] is None
            assert signal_row["tokens_in"] == 2
            assert signal_row["token_usage_complete"] is False


if __name__ == "__main__":
    test_unknown_valuation_readers_preserve_missing_counts()

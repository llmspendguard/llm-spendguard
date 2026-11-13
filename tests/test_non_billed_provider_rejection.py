"""A provider payload rejection is observable without becoming money or a reconcile leak."""
import json
import sys
import types

import pytest

from spendguard import adapters, budget, config, focus_export, ledger
from spendguard.analyze.reconciliation.reconcile_estimate_stage import stage_reconcile_estimate_cells
from spendguard.store.spend_search.ledger_read import (
    read_spend_aggregates,
    read_spend_events,
    read_spend_since,
)


PROVIDER_REJECTION_STATUS = 400
PROVIDER_REJECTION_PROVIDER = "anthropic"
PROVIDER_REJECTION_MODEL = "claude-haiku-4-5"
PROVIDER_REJECTION_PROMPT = "offline rejection fixture"
PROVIDER_REJECTION_TOKEN_BUDGET = 8
PROVIDER_REJECTION_KEY = "test-key-not-real"
PROVIDER_REJECTION_SINCE = "2000-01-01"
PROVIDER_REJECTION_PROJECT = "non-billed-rejection-guard"


class ProviderPayloadRejection(Exception):
    status_code = PROVIDER_REJECTION_STATUS


class RejectingProviderStream:
    def __enter__(self):
        raise ProviderPayloadRejection("synthetic provider payload rejection")

    def __exit__(self, *args):
        return False


class RejectingProviderMessages:
    def stream(self, **kwargs):
        return RejectingProviderStream()


class RejectingProviderClient:
    def __init__(self, *args, **kwargs):
        self.messages = RejectingProviderMessages()


def test_provider_rejection_is_observable_and_absent_from_every_money_reader(tmp_path, monkeypatch):
    ledger_path = tmp_path / "provider-rejection.sqlite"
    writer = ledger.SpendLedger(ledger_path, owned_connection=config.open_ledger_connection(ledger_path))
    fake_anthropic = types.ModuleType("anthropic")
    fake_anthropic.Anthropic = RejectingProviderClient
    monkeypatch.setitem(sys.modules, "anthropic", fake_anthropic)
    monkeypatch.setenv("ANTHROPIC_API_KEY", PROVIDER_REJECTION_KEY)
    monkeypatch.setattr(config, "db_path", lambda: ledger_path)
    monkeypatch.setattr(budget._LEDGER_TL, "led", writer, raising=False)
    monkeypatch.setattr(budget, "_project", lambda: PROVIDER_REJECTION_PROJECT)

    result = adapters._call_once(PROVIDER_REJECTION_MODEL, PROVIDER_REJECTION_PROMPT,
                                 max_tokens=PROVIDER_REJECTION_TOKEN_BUDGET, _skip_lane=True)

    assert result["status_code"] == PROVIDER_REJECTION_STATUS
    assert result["failure_kind"] == adapters.FAILURE_KIND_CLIENT_BUG
    events = read_spend_events(since=PROVIDER_REJECTION_SINCE, path=ledger_path)
    assert len(events) == 1
    event = events[0]
    assert event["cost_type"] == ledger.NON_BILLED_FAILURE_KIND
    assert event["billed"] == 0
    assert event["provider"] == PROVIDER_REJECTION_PROVIDER
    assert event["model"] == PROVIDER_REJECTION_MODEL
    assert event["in_tok"] == event["out_tok"] == 0
    assert all(event[column] in (None, "0", 0) for column in ledger.USD_COLS)
    failure_tags = json.loads(event["tags"])
    assert failure_tags["status_code"] == PROVIDER_REJECTION_STATUS
    assert failure_tags["failure_kind"] == adapters.FAILURE_KIND_CLIENT_BUG

    aggregates = read_spend_aggregates(since=PROVIDER_REJECTION_SINCE, path=ledger_path)
    assert len(aggregates) == 1
    aggregate = aggregates[0]
    assert aggregate["n"] == 0
    assert all(aggregate[column] == 0 for column in ledger.USD_COLS)
    assert read_spend_since(PROVIDER_REJECTION_SINCE) == 0

    with stage_reconcile_estimate_cells(
            PROVIDER_REJECTION_SINCE, budget._TRUE_DOWN_CONV, lambda model: model, path=ledger_path) as (staged, gaps):
        assert staged.execute("SELECT COUNT(*) FROM cells").fetchone()[0] == 0
        assert gaps == {}

    assert focus_export.focus_row(dict(event)) is None
    assert focus_export.export_rows(since=PROVIDER_REJECTION_SINCE) == []
    with pytest.raises(ValueError):
        writer.category_col(ledger.NON_BILLED_FAILURE_KIND)
    assert ledger.NON_BILLED_FAILURE_KIND not in ledger._KIND_TO_USD
    assert ledger.NON_BILLED_FAILURE_KIND not in focus_export._BILLED_KINDS
    writer._conn.close()

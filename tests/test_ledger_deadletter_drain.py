"""Durable, idempotent writer replay and bounded failure preservation, offline."""
import json
import os
import sys
import tempfile
from pathlib import Path

if not os.environ.get("SPENDGUARD_TEST_ISOLATED"):
    os.environ["SPENDGUARD_TEST_ISOLATED"] = "1"
    os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="ledger-deadletter-drain-")
    os.execv(sys.executable, [sys.executable, "-S"] + sys.argv)
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from spendguard import budget, config
from spendguard.ledger import SpendLedger
from spendguard.ledger_deadletter import (append_spend_deadletter_event, drain_spend_deadletter_events,
                                         LEDGER_DEADLETTER_RETRY_LIMIT)

DEADLETTER_FIXTURE_COST = "0.017"
DEADLETTER_FIXTURE_TOKEN_COUNT = 17
DEADLETTER_FIXTURE_CHAIN = "offline-replay-chain"
DEADLETTER_FIXTURE_INTENT = "offline:replay"
DEADLETTER_FIXTURE_PROJECT = "offline-replay-project"
DEADLETTER_FIXTURE_PROVIDER = "offline-replay-provider"
DEADLETTER_FIXTURE_MODEL = "offline-replay-model"


class RejectCaptureFixtureLedger:
    def record_event(self, event, **kwargs):
        raise OSError("offline injected commit failure")


previous_ledger_factory = budget._ledger
budget._ledger = lambda: RejectCaptureFixtureLedger()
try:
    budget._record_spend_event(DEADLETTER_FIXTURE_PROVIDER, DEADLETTER_FIXTURE_MODEL, "realtime",
                               DEADLETTER_FIXTURE_COST, intent=DEADLETTER_FIXTURE_INTENT,
                               chain=DEADLETTER_FIXTURE_CHAIN, project=DEADLETTER_FIXTURE_PROJECT,
                               reasoning_tok=DEADLETTER_FIXTURE_TOKEN_COUNT)
finally:
    budget._ledger = previous_ledger_factory

spool_path = budget.spend_event_retry_paths()[0]
payload = json.loads(spool_path.read_text())
assert payload["event"]["chain"] == DEADLETTER_FIXTURE_CHAIN
assert payload["event"]["reasoning_tok"] == DEADLETTER_FIXTURE_TOKEN_COUNT
report = drain_spend_deadletter_events([spool_path])
assert report == {"committed": 1, "retained": 0, "errors": []}, report
assert spool_path.read_text() == ""
append_spend_deadletter_event(spool_path, payload)
assert drain_spend_deadletter_events([spool_path])["committed"] == 1
ledger = SpendLedger()
try:
    assert ledger.count_events() == 1
finally:
    if ledger._conn is not None:
        ledger._conn.close()

original_record_event = SpendLedger.record_event


def reject_deadletter_retry_fixture(self, event, **kwargs):
    raise OSError("offline persistent commit failure")


SpendLedger.record_event = reject_deadletter_retry_fixture
append_spend_deadletter_event(spool_path, payload)
retry_limit = int(config._cfg_get("ledger", "deadletter_retry_limit", LEDGER_DEADLETTER_RETRY_LIMIT))
try:
    for _ in range(retry_limit + 1):
        assert drain_spend_deadletter_events([spool_path])["retained"] == 1
    assert json.loads(spool_path.read_text())["retry_attempts"] == retry_limit
finally:
    SpendLedger.record_event = original_record_event
print("PASS: durable deadletters replay idempotently; failed writes retained with bounded retries")

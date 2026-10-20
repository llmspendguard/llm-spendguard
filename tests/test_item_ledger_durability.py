"""Fault guards for durable settlements, named damage, and retry epochs (no model calls)."""
import os
import tempfile
from pathlib import Path
from unittest.mock import patch

from spendguard.item_ledger import ANSWERED, ItemLedger

LEDGER_GUARD_ITEMS = tuple(range(4))
LEDGER_GUARD_ROUNDS = 2
LEDGER_GUARD_FILENAME = "settlements.jsonl"
LEDGER_GUARD_MISSING_ITEM_ROW = b'{"state": "answered"}\n'


def test_item_ledger_torn_tail_retry_durability():
    with tempfile.TemporaryDirectory() as directory:
        ledger_path = Path(directory) / LEDGER_GUARD_FILENAME
        ledger = ItemLedger(LEDGER_GUARD_ITEMS, path=ledger_path)
        with patch("spendguard.item_ledger.os.fsync", wraps=os.fsync) as synced:
            for item in LEDGER_GUARD_ITEMS:
                ledger.settle_item(item, ANSWERED, answer=str(item))
            assert synced.call_count >= len(LEDGER_GUARD_ITEMS)
        complete_rows = ledger_path.read_bytes().splitlines(keepends=True)
        torn_row = complete_rows[-1][:len(complete_rows[-1]) // 2]
        ledger_path.write_bytes(b"".join(complete_rows[:-1]) + torn_row)
        damaged_bytes = ledger_path.read_bytes()
        restarted = ItemLedger(LEDGER_GUARD_ITEMS, path=ledger_path)
        assert restarted.state == dict.fromkeys(LEDGER_GUARD_ITEMS[:-1], ANSWERED)
        assert len(restarted.recovery_gaps) == 1
        assert restarted.recovery_gaps[0]["raw"] == torn_row
        assert restarted.recovery_gaps[0]["line"] == len(LEDGER_GUARD_ITEMS)
        assert restarted.recovery_gaps[0]["traceback"]
        for operation in (restarted.pending, restarted.assert_complete,
                          lambda: restarted.settle_item(LEDGER_GUARD_ITEMS[-1], ANSWERED)):
            try:
                operation()
            except RuntimeError as exc:
                assert "recovery gaps" in str(exc)
            else:
                raise AssertionError("Damaged settlement was treated as absent")
        assert ledger_path.read_bytes() == damaged_bytes

    # Mutation: restoring the missing-item silent continue must fail the named-gap assertion.
    with tempfile.TemporaryDirectory() as directory:
        ledger_path = Path(directory) / LEDGER_GUARD_FILENAME
        ledger_path.write_bytes(LEDGER_GUARD_MISSING_ITEM_ROW)
        restarted = ItemLedger(LEDGER_GUARD_ITEMS, path=ledger_path)
        assert restarted.recovery_gaps == [{"path": str(ledger_path), "line": 1,
                                            "raw": LEDGER_GUARD_MISSING_ITEM_ROW,
                                            "reason": "missing item field"}]
        try:
            restarted.pending()
        except RuntimeError as exc:
            assert "missing item field" in str(exc)
        else:
            raise AssertionError("Missing-item row was treated as absent")

    with tempfile.TemporaryDirectory() as directory:
        ledger_path = Path(directory) / LEDGER_GUARD_FILENAME
        ledger = ItemLedger(LEDGER_GUARD_ITEMS, path=ledger_path)
        for _ in range(LEDGER_GUARD_ROUNDS):
            ledger.note_attempt(LEDGER_GUARD_ITEMS)
        ledger.close_exhausted(rounds=LEDGER_GUARD_ROUNDS)
        ledger.reopen()
        restarted = ItemLedger(LEDGER_GUARD_ITEMS, path=ledger_path)
        assert not restarted.exhausted(rounds=LEDGER_GUARD_ROUNDS)
        for _ in range(LEDGER_GUARD_ROUNDS):
            restarted.note_attempt(LEDGER_GUARD_ITEMS)
        exhausted_restart = ItemLedger(LEDGER_GUARD_ITEMS, path=ledger_path)
        assert exhausted_restart.exhausted(rounds=LEDGER_GUARD_ROUNDS) == list(LEDGER_GUARD_ITEMS)
        with patch("spendguard.item_ledger.os.fsync", side_effect=OSError("injected sync failure")):
            try:
                exhausted_restart.settle_item(LEDGER_GUARD_ITEMS[0], ANSWERED)
            except OSError:
                pass
            else:
                raise AssertionError("Sync failure was swallowed")
        assert not exhausted_restart.state


if __name__ == "__main__":
    test_item_ledger_torn_tail_retry_durability()
    print("item ledger durability guards passed")

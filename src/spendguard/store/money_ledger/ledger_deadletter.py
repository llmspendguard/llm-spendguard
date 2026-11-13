"""Durable spend capture retry, replayed through the ledger's sole writer."""
import contextlib
import fcntl
import hashlib
import json
import os
import tempfile
import time
from pathlib import Path
from decimal import Decimal

from ... import config
from .ledger_writer import (serialize_ledger_function, require_ledger_writer_request_live,
                            LEDGER_WRITER_DEADLINE_POLL_DIVISOR, current_ledger_writer_connection,
                            isolate_nested_ledger_write, ledger_connection_commit_deferred)

LEDGER_DEADLETTER_RETRY_LIMIT = 3
LEDGER_DEADLETTER_LOCK_SUFFIX = ".lock"
LEDGER_DEADLETTER_OUTCOME_SUFFIX = ".outcomes.jsonl"
LEDGER_DEADLETTER_ZERO_USD = Decimal("0")


@contextlib.contextmanager
def locked_spend_deadletter_path(path):
    retry_path = Path(path)
    retry_path.parent.mkdir(parents=True, exist_ok=True)
    with open(str(retry_path) + LEDGER_DEADLETTER_LOCK_SUFFIX, "a") as lock_handle:
        from spendguard.store.spend_search.ledger_read_accessors import resolve_current_ledger_request_scope as current_ledger_request_scope
        scope = current_ledger_request_scope()
        if scope is None:
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
        else:
            while True:
                require_ledger_writer_request_live()
                try:
                    fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    time.sleep(max(0, scope.deadline_at - time.monotonic())
                               / LEDGER_WRITER_DEADLINE_POLL_DIVISOR)
        try:
            require_ledger_writer_request_live()
            yield retry_path
        finally:
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)


def append_spend_deadletter_event(path, payload):
    """Return only after the complete retry evidence is durably written."""
    from spendguard.store.spend_search.ledger_read_accessors import resolve_ledger_evidence_persistence_scope as ledger_evidence_persistence_scope
    with ledger_evidence_persistence_scope(), locked_spend_deadletter_path(path) as retry_path:
        with open(retry_path, "a") as spool:
            spool.write(json.dumps(payload) + "\n")
            spool.flush()
            os.fsync(spool.fileno())


@serialize_ledger_function
def drain_spend_deadletter_events(paths=None):
    """Commit retries before removing them; retain malformed/exhausted evidence."""
    with contextlib.ExitStack() as spool_locks:
        return stage_and_commit_spend_deadletters(paths, spool_locks)


def stage_and_commit_spend_deadletters(paths, spool_locks):
    """Keep every spool locked until the one authoritative replay commit."""
    from spendguard.manage.admission.admission_accessors import resolve_spend_event_retry_paths as spend_event_retry_paths, resolve_charge_to_event as charge_to_event, resolve__attribute as _attribute
    from .ledger import SpendLedger
    from spendguard.store.spend_search.ledger_read_contracts import LedgerQueryDeadline
    writer_connection = current_ledger_writer_connection()
    if ledger_connection_commit_deferred(writer_connection):
        raise RuntimeError("Deadletter drain requires ownership of its final commit")
    retry_limit = int(config._cfg_get("ledger", "deadletter_retry_limit", LEDGER_DEADLETTER_RETRY_LIMIT))
    if retry_limit <= 0:
        raise ValueError("ledger.deadletter_retry_limit must be positive")
    report = {"committed": 0, "retained": 0, "errors": []}
    staged_spools = []
    locked_spool_paths = set()
    for candidate in paths if paths is not None else spend_event_retry_paths():
        candidate = Path(candidate).resolve()
        if candidate in locked_spool_paths or not candidate.exists():
            continue
        locked_spool_paths.add(candidate)
        with contextlib.nullcontext(spool_locks.enter_context(locked_spend_deadletter_path(candidate))) as retry_path:
            retained = []
            recovered_events = []
            retention_reasons = []
            with retry_path.open(newline="") as original_spool:
                spool_lines = original_spool.read().splitlines(keepends=True)
            rollback_lines = list(spool_lines)
            for line_number, line in enumerate(spool_lines):
                payload = None
                write_attempted = False
                try:
                    require_ledger_writer_request_live()
                    payload = json.loads(line)
                    attempts = int(payload.get("retry_attempts", 0))
                    if attempts >= retry_limit:
                        retained.append(line)
                        retention_reasons.append("retry limit exhausted; evidence retained")
                        continue
                    event = payload.get("event")
                    if event is None:
                        event = charge_to_event(payload["provider"], payload["model"], payload["kind"], payload["cost"],
                                                intent=payload.get("intent", ""))
                        _attribute(event, payload.get("project", ""))
                        for field in ("in_tok", "out_tok", "occurred_at", "source"):
                            if payload.get(field) is not None:
                                event[field] = payload[field]
                        event["dedup_key"] = hashlib.sha256(
                            (str(retry_path) + str(line_number) + line).encode()).hexdigest()
                        payload["event"] = event
                    ledger = SpendLedger()
                    try:
                        require_ledger_writer_request_live()
                        with isolate_nested_ledger_write(current_ledger_writer_connection()):
                            write_attempted = True
                            ledger.record_event(event, venv=payload.get("venv"))
                    finally:
                        if ledger._conn is not None:
                            ledger._conn.close()
                    recovered_events.append(event)
                    report["committed"] += 1
                except LedgerQueryDeadline as deadline_error:
                    report["errors"].append(repr(deadline_error))
                    retention_reasons.append(repr(deadline_error))
                    retained.extend(spool_lines[line_number:])
                    break
                except Exception as retry_error:
                    report["errors"].append(repr(retry_error))
                    retention_reasons.append(repr(retry_error))
                    try:
                        if not write_attempted:
                            retained.append(line)
                            continue
                        payload["retry_attempts"] = int(payload.get("retry_attempts", 0)) + 1
                        payload["retry_error"] = repr(retry_error)
                        rollback_lines[line_number] = json.dumps(payload) + "\n"
                        retained.append(rollback_lines[line_number])
                    except (UnboundLocalError, TypeError, AttributeError, ValueError):
                        retained.append(line)
            staged_spools.append((retry_path, retained, rollback_lines, recovered_events, retention_reasons))
    committed = False
    finalization_error = None
    try:
        require_ledger_writer_request_live()
        if getattr(writer_connection, "_ledger_writer_transaction_aborted", False):
            raise RuntimeError("Deadletter replay transaction was aborted")
        if writer_connection.in_transaction:
            writer_connection.commit()
        committed = True
    except BaseException as commit_error:
        finalization_error = commit_error
        from .ledger_writer import abort_current_ledger_writer_transaction
        abort_current_ledger_writer_transaction()
    for retry_path, retained, rollback_lines, recovered_events, retention_reasons in staged_spools:
        if not committed:
            retained = rollback_lines
        # Record the outcome before shrinking the queue; failed journaling leaves
        # original evidence available for a safe, deduplicated replay.
        if committed and recovered_events:
            persist_spend_deadletter_outcome(retry_path, "recovered", recovered_events,
                                             "ledger replay committed")
        if retained:
            persist_spend_deadletter_outcome(
                retry_path, "retained", retained,
                repr(finalization_error) if not committed else "; ".join(retention_reasons))
        report["retained"] += len(retained)
        replace_spend_deadletter_spool(retry_path, retained)
    if not committed:
        report["committed"] = 0
    if finalization_error is not None:
        raise finalization_error
    return report


def replace_spend_deadletter_spool(retry_path, retained):
    """Durably publish staged evidence while its caller retains the spool lock."""
    descriptor, replacement = tempfile.mkstemp(dir=retry_path.parent)
    try:
        with os.fdopen(descriptor, "w", newline="") as spool:
            spool.writelines(retained)
            spool.flush()
            os.fsync(spool.fileno())
        os.replace(replacement, retry_path)
        directory_fd = os.open(retry_path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if os.path.exists(replacement):
            os.unlink(replacement)


def persist_spend_deadletter_outcome(retry_path, outcome, evidence, reason):
    """Fsync a separate recovery history before the corresponding spool shrinks."""
    billed_usd = LEDGER_DEADLETTER_ZERO_USD
    est_sub_value_usd = LEDGER_DEADLETTER_ZERO_USD
    unknown_amount_count = 0
    for item in evidence:
        try:
            payload = json.loads(item) if isinstance(item, str) else item
            event = payload.get("event", payload)
            amount = event.get("usd", payload.get("cost"))
            if amount is None:
                unknown_amount_count += 1
            elif event.get("billed", 1):
                billed_usd += Decimal(str(amount))
            else:
                est_sub_value_usd += Decimal(str(amount))
        except (ValueError, TypeError, AttributeError, ArithmeticError):
            unknown_amount_count += 1
    outcome_path = retry_path.with_name(retry_path.name + LEDGER_DEADLETTER_OUTCOME_SUFFIX)
    append_spend_deadletter_event(outcome_path, {
        "outcome": outcome, "count": len(evidence), "billed_usd": str(billed_usd),
        "est_sub_value_usd": str(est_sub_value_usd), "unknown_amount_count": unknown_amount_count,
        "reason": reason, "recorded_at": time.time(), "spool_path": str(retry_path),
    })
    directory_fd = os.open(outcome_path.parent, os.O_RDONLY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)

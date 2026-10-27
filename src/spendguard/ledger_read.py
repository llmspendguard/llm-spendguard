"""Bounded, admission-counted, read-only ledger requests shared by every skin."""
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from pathlib import Path
import sqlite3
import time
import weakref
import logging
import traceback
import threading
import math
import datetime
import functools
import inspect

from . import config
from .ledger_deadline_polling import LedgerDeadlinePolling

CC_RESIDENCY_SOURCE = "claude-code"
CC_RESIDENCY_LATEST_SQL = "SELECT conv_id FROM spend_events WHERE source=? ORDER BY occurred_at DESC,conv_id DESC LIMIT 1"
CC_RESIDENCY_STREAM_SQL = "SELECT rowid,model,cache_read_tok FROM spend_events WHERE source=? AND conv_id=? ORDER BY occurred_at,rowid"
CC_RESIDENCY_TOKEN_SCALE = 1000000
_cc_residency_logger = logging.getLogger(__name__)

# A complete result must fit ordinary interactive consumers; larger consumers must
# narrow their window or use count/aggregates, never silently consume a head.
# Long enough for local reporting, bounded independently of connection lock waits.


LEDGER_EVENTS_TABLE = "spend_events"
LEDGER_CALLS_TABLE = "calls"
SETTLEMENT_GUARDED_EVIDENCE_TABLES = (LEDGER_CALLS_TABLE, "decisions")
LEDGER_INSIGHTS_TABLE = "insights"
DEFAULT_LEDGER_READ_ROW_CAP = config.LEDGER_READ_ROW_CAP
LEDGER_READ_OVERFLOW_PROBE_ROW_COUNT = 1


def _default_ledger_read_row_cap():
    """Resolve the read row cap at REQUEST-BUILD time by reading the module global LIVE, instead of freezing it
    into a dataclass field default or a function arg default at class/def-definition time. The two are identical
    in production — DEFAULT_LEDGER_READ_ROW_CAP is set once from config and never rebound — but a frozen default
    captures whatever config happened to resolve at the instant this module was first imported, which makes a
    test that lowers the cap AFTER import (to exercise the scale-invariant containment boundary at ~50 rows
    rather than scanning the 10,000-row production default) silently ineffective when an outer harness imported
    config first. Late-binding here makes DEFAULT_LEDGER_READ_ROW_CAP the single live lever, independent of
    import order."""
    return DEFAULT_LEDGER_READ_ROW_CAP


DEFAULT_LEDGER_READ_TIME_BUDGET_S = config.LEDGER_READ_TIME_BUDGET_S
# sqlite3.set_progress_handler fires the callback every N SQLite virtual-machine instructions
# (opcodes), NOT N "progress steps" — this is that instruction interval, the wall-clock budget's
# polling granularity. Smaller = more frequent timeout checks at a small per-instruction cost.
DEFAULT_LEDGER_READ_PROGRESS_VM_INSTRUCTIONS = config.LEDGER_READ_PROGRESS_VM_INSTRUCTIONS
LEDGER_READ_DECIMAL_ARITY = 1
LEDGER_READ_DISABLE_PROGRESS_VM_INSTRUCTIONS = 0
# SQLite extended result codes retain their primary result in the low byte.
LEDGER_READ_SQLITE_PRIMARY_CODE_MASK = 0xFF
LEDGER_READ_COUNT_EXPRESSION = "COUNT(*)"
LEDGER_EVIDENCE_SNAPSHOT_SAVEPOINT = "ledger_evidence_snapshot"
SPEND_EVENT_READ_COLUMNS = tuple("""id dedup_key source content_hash schema_version ts_utc occurred_at recorded_at tz local_datetime day period eligibility_window window_start window_end currency batch_usd realtime_usd est_chat_usd remote_compute_usd subscription_usd external_usd cost_type billed is_meta cost_basis amount_confidence rate_in rate_out fx_rate base_usd provider model model_kind finish in_tok out_tok cache_read_tok cache_write_tok reasoning_tok num_calls num_items latency org team projects project_primary member_ref key_fp account_id customer_id cost_center engagement billable invoice_id conv_id seg_id call_id cwd batch_id from_message_ids prior_message_ids post_message_ids script repo host prompt_hash prompt_snip output_snip evidence_uri intent actor chain attr_what attr_why attr_how attr_reason attr_confidence attr_source attr_model attr_ts attr_version recorded_by ingest_version status revision locked locked_at lock_reason reverses_id adjusts_id superseded_by reconciled reconciled_vs reconciled_at reconciliation_id gap_flag period_closed recon_marker quality quality_src quality_conf cache_hit savings_cv tags""".split())
RECENT_CALL_READ_COLUMNS = tuple("id ts chain intent caller provider model kind in_tok out_tok cost latency finish quality executor project effort outcome disposition".split())
LEDGER_READ_ALLOWED_TABLES = (LEDGER_EVENTS_TABLE, LEDGER_CALLS_TABLE, LEDGER_INSIGHTS_TABLE)
LEDGER_READ_AGGREGATE_DIMENSIONS = (("provider", "day"), ("intent",), ("model",))
ADVISOR_EVIDENCE_COLUMNS = tuple("provider model intent cost in_tok out_tok quality quality_conf effort".split())
ADVISOR_INTERNAL_INTENT_PATTERN = "spendguard:%"
INVOICE_OVERAGE_SOURCE = "anthropic-invoice"
INVOICE_OVERAGE_INTENT_PATTERN = "anthropic-invoice:cc-overage%"
DELEGATE_INVOICED_MONTHS_SQL = """SELECT substr(occurred_at,1,7) AS month,
SUM(CAST(realtime_usd AS REAL)) AS dollars FROM spend_events
WHERE source=? AND intent LIKE ? GROUP BY month"""
DELEGATE_PLAN_SOURCE = "claude-code"
INVOICE_OVERAGE_MONTH_SQL = """SELECT substr(occurred_at,1,7) AS mo,
ROUND(SUM(CAST(realtime_usd AS REAL)),2) AS amount FROM spend_events
WHERE source=? AND intent LIKE ? GROUP BY mo ORDER BY mo DESC"""
COST_INTEGRITY_CACHE_COLUMNS = ("cache_read_tok", "cache_write_tok")
COST_INTEGRITY_SCHEMA_SQL = "SELECT name FROM pragma_table_info(?)"
COST_INTEGRITY_BUCKET_SQL = """SELECT model,date(ts) AS d,kind,COUNT(*) AS n,
SUM(COALESCE(cost,0)) AS c,{input_projection} AS i,
CASE WHEN COUNT(out_tok)=COUNT(*) THEN SUM(out_tok) END AS o,
{cache_read_projection} AS cache_read_tok,
{cache_write_projection} AS cache_write_tok FROM calls
WHERE ts>=? AND kind IN ('realtime','batch')
AND cost IS NOT NULL AND suspect IS NULL GROUP BY model,date(ts),kind{cache_completeness_group}"""
RECEIPT_LANE_ACTIVITY_SQL = """SELECT executor,COUNT(*) AS n,
CASE WHEN COUNT(in_tok)=COUNT(*) THEN COALESCE(SUM(in_tok),0) END AS in_tok,
CASE WHEN COUNT(out_tok)=COUNT(*) THEN COALESCE(SUM(out_tok),0) END AS out_tok
FROM calls WHERE kind='subscription' AND ts>=? GROUP BY executor ORDER BY n DESC"""
RECEIPT_TRUNCATION_GROUPS_SQL = """SELECT model,COUNT(*) AS n,
COALESCE(SUM(cost),0) AS cost FROM calls WHERE finish IN ('max_tokens','length')
AND ts>=? GROUP BY model ORDER BY cost DESC"""
CC_SPEND_OVERVIEW_SQL = """SELECT
COALESCE(SUM(CASE WHEN source='claude-code' THEN CAST(est_chat_usd AS REAL) ELSE 0 END),0) AS est,
COALESCE(SUM(CASE WHEN source='anthropic-invoice' AND intent LIKE 'anthropic-invoice:cc-overage%' THEN CAST(realtime_usd AS REAL) ELSE 0 END),0) AS over,
COALESCE(SUM(CASE WHEN source='anthropic-invoice' THEN CAST(subscription_usd AS REAL) ELSE 0 END),0) AS sub,
COALESCE(SUM(CASE WHEN source='anthropic-invoice-api' THEN CAST(realtime_usd AS REAL) ELSE 0 END),0) AS api
FROM spend_events"""
CC_TOP_CONVERSATIONS_SQL = """SELECT conv_id,ROUND(SUM(CAST(est_chat_usd AS REAL)),2) AS v,COUNT(*) AS n,
MAX(COALESCE(in_tok,0)+COALESCE(cache_read_tok,0)+COALESCE(cache_write_tok,0)) AS mx
FROM spend_events WHERE source='claude-code' GROUP BY conv_id ORDER BY v DESC"""
CC_CONVERSATION_COST_SQL = """SELECT
COALESCE(SUM(CASE WHEN source='claude-code' THEN CAST(est_chat_usd AS REAL) ELSE 0 END),0) AS est,
COUNT(CASE WHEN source='claude-code' THEN 1 END) AS n,
COALESCE(SUM(CASE WHEN source='claude-code-overflow' THEN CAST(realtime_usd AS REAL) ELSE 0 END),0) AS over
FROM spend_events WHERE conv_id=?"""
CALL_INTENTS_SQL = "SELECT intent,COUNT(*) AS c FROM calls WHERE intent IS NOT NULL AND intent!='' GROUP BY intent HAVING c>=? ORDER BY c DESC"
CALL_MAX_ROWID_SQL = "SELECT COALESCE(MAX(rowid),0) AS maximum FROM calls"
CALL_FLOW_COLUMNS = ("COUNT(*) AS n", "CASE WHEN COUNT(in_tok)=COUNT(*) THEN COALESCE(SUM(in_tok),0) END AS in_tok", "CASE WHEN COUNT(out_tok)=COUNT(*) THEN COALESCE(SUM(out_tok),0) END AS out_tok", "COALESCE(SUM(cost),0.0) AS cost", "MAX(caller) AS caller")
CALL_COST_COLUMNS = ("COALESCE(intent,'(none)') AS intent", "COALESCE(model,'?') AS model", "COUNT(*) AS n", "COALESCE(SUM(cost),0) AS cost", "SUM(CASE WHEN quality='good' THEN 1 ELSE 0 END) AS good", "SUM(CASE WHEN quality='bad' THEN 1 ELSE 0 END) AS bad")
CALL_EXECUTOR_EXPRESSION = "COALESCE(NULLIF(NULLIF(executor,''),'api'), provider)"
CALL_MODEL_EXPRESSION = "COALESCE(model,'?')"
CALL_OUTCOME_EXPRESSION = "COALESCE(outcome,'')"
CALL_METERED_COLUMNS = ("COALESCE(intent,'(none)') AS intent", "COALESCE(model,'?') AS model", "COUNT(*) AS n", "COALESCE(SUM(cost),0) AS cost", "CASE WHEN COUNT(in_tok)=COUNT(*) THEN COALESCE(SUM(in_tok),0) END AS in_tok", "CASE WHEN COUNT(in_tok)=COUNT(*) THEN COALESCE(AVG(in_tok),0) END AS avg_in")
LANE_SUBSCRIPTION_READ_COLUMNS = ("executor", "ts", "provider", "model", "in_tok", "out_tok", "project")
CALL_KNOWN_INTENT_COLUMN = ("DISTINCT intent",)
LANE_TOKEN_METRIC_COLUMNS = ("COUNT(*) AS n", "CASE WHEN COUNT(in_tok+out_tok)=COUNT(*) THEN SUM(in_tok+out_tok) END AS total", "CASE WHEN COUNT(in_tok)=COUNT(*) THEN SUM(in_tok) END AS input", "CASE WHEN COUNT(out_tok)=COUNT(*) THEN SUM(out_tok) END AS output")
PROMPT_LINT_READ_COLUMNS = ("intent", "model", "in_tok", "out_tok", "cost", "finish", "prompt_snip", "kind")
LEDGER_ROUTING_METERED_SQL = "cost > 0 AND COALESCE(NULLIF(executor,''),'') IN ('','api')"
LEDGER_ROUTING_EXECUTOR_SQL = "COALESCE(NULLIF(executor,''),'unknown')"
LEDGER_ROUTING_LANE_SQL = "COALESCE(NULLIF(resolved_lane,''),'unknown')"
LEDGER_ROUTING_SERVED_SQL = "COALESCE(NULLIF(served_model,''),'unknown')"
CALIBRATION_CALL_COLUMNS = ("intent", "model", "kind", "in_tok", "out_tok", "cost", "ts")
CALIBRATION_ACTUAL_COLUMNS = ("in_tok", "out_tok", "cost", "ts", "model")
CALIBRATION_CORPUS_PREDICATE = "cost > 0 AND in_tok > 0 AND out_tok > 0 AND model IS NOT NULL"
CC_OVERFLOW_COLUMNS = ("conv_id", "SUM(CAST(realtime_usd AS REAL)) AS billed")
CC_ATTRIBUTION_MONTH_COLUMNS = ("substr(occurred_at,1,7) AS mo", "SUM(CAST(realtime_usd AS REAL)) AS billed")
CC_ATTRIBUTION_WINDOW_COLUMNS = ("substr(occurred_at,1,7) AS mo", "conv_id",
                                 "SUM(CAST(est_chat_usd AS REAL)) AS est")
CC_ATTRIBUTION_FALLBACK_COLUMNS = ("substr(occurred_at,1,7) AS mo", "conv_id", "SUM(CAST(est_chat_usd AS REAL)) AS est")
CC_CONVERSATION_TURN_COLUMNS = ("conv_id", "occurred_at", "model", "COALESCE(in_tok,0)", "COALESCE(cache_read_tok,0)", "COALESCE(cache_write_tok,0)")
CC_ESTIMATE_PREDICATE = "source='claude-code' AND est_chat_usd IS NOT NULL"
READ_CALL_INTENT_COST_COLUMNS = ("COALESCE(SUM(cost),0) AS cost",)
COUNT_RECENT_CALL_TESTS_COLUMNS = ("id",)
READ_LANE_RECENT_COUNTS_COLUMNS = ("executor", "COUNT(*) AS n")
READ_INTENT_MODEL_USAGE_COLUMNS = ("model", "COUNT(*) AS n")
READ_BAKEOFF_MEASUREMENTS_COLUMNS = ("model", "quality", "ts")
READ_LANE_FALLBACK_COSTS_COLUMNS = ("fell_from", "COUNT(*) AS n", "COALESCE(SUM(cost),0) AS cost")
READ_LANE_CUMULATIVE_TOKENS_COLUMNS = ("executor", "CASE WHEN COUNT(in_tok+out_tok)=COUNT(*) THEN SUM(in_tok+out_tok) END AS tokens")
READ_DELEGATE_OVERAGE_EVIDENCE_COLUMNS = ("SUM(CAST(est_chat_usd AS REAL)) AS value",)
READ_REALIZED_ADOPTIONS_COLUMNS = ("intent", "MIN(ts) AS adopted_ts")
READ_REALIZED_CALL_RATES_COLUMNS = ("AVG(CASE WHEN ts < ? THEN cost END) AS before_rate", "SUM(ts < ?) AS before_n",
         "AVG(CASE WHEN ts >= ? THEN cost END) AS after_rate", "SUM(ts >= ?) AS after_n")
READ_RECORDING_GAP_CELLS_COLUMNS = ("intent", "provider", "model", "date(ts) AS day", "SUM(cost) AS calls_billed")
READ_CALIBRATION_SUMMARY_COLUMNS = ("intent", "model", "kind", "COUNT(*) AS n", "SUM(cost) AS cost")
RECORDED_CALL_TOTAL_SQL = "SELECT COALESCE(SUM(cost),0) AS recorded_call_total FROM calls"
INCOMPLETE_CALL_SCHEMA_COLUMNS = ("settlement_complete", "input_cost_floor")
INCOMPLETE_CALL_TOTALS_SQL = """SELECT COUNT(*) AS n,
SUM(input_cost_floor) AS input_cost_floor FROM calls WHERE settlement_complete=0"""


@dataclass
class _LedgerReadProcessMetrics:
    """Own the synchronized, process-local ledger reader accounting state."""

    _read_attempt_lock: object = field(default_factory=threading.Lock)
    _read_attempt_count: int = 0
    _read_elapsed_seconds: float = 0.0

    @contextmanager
    def observe_ledger_read_attempt(self):
        observation_started = time.monotonic()
        with self._read_attempt_lock:
            self._read_attempt_count += 1
        try:
            yield
        finally:
            with self._read_attempt_lock:
                self._read_elapsed_seconds += time.monotonic() - observation_started

    def snapshot_ledger_read_process_metrics(self):
        with self._read_attempt_lock:
            return {"open_connections": config.ledger_connection_count(),
                    "read_calls_issued": self._read_attempt_count,
                    "cumulative_read_seconds": self._read_elapsed_seconds}


_LEDGER_READ_PROCESS_METRICS = _LedgerReadProcessMetrics()


def _reset_ledger_read_metrics_in_child():
    global _LEDGER_READ_PROCESS_METRICS
    _LEDGER_READ_PROCESS_METRICS = _LedgerReadProcessMetrics()


if hasattr(config.os, "register_at_fork"):
    config.os.register_at_fork(after_in_child=_reset_ledger_read_metrics_in_child)


@contextmanager
def _ledger_read_observation():
    """Account for one issued read, including setup, failure and cleanup time."""
    with _LEDGER_READ_PROCESS_METRICS.observe_ledger_read_attempt():
        yield


def ledger_read_metrics():
    """Return process-local read attempts, elapsed seconds and managed connections."""
    return _LEDGER_READ_PROCESS_METRICS.snapshot_ledger_read_process_metrics()


def read_cc_residency_totals(conv_id=None, path=None):
    """Fold every selected ledger row with constant row memory; expose corrupt units."""
    from . import pricing

    residency_turns = 0
    residency_measured = 0.0
    residency_model = None
    residency_gaps = []
    residency_rates = {}
    try:
        with _ledger_read_connection(path) as residency_connection:
            if conv_id is None:
                residency_latest = residency_connection.execute(
                    CC_RESIDENCY_LATEST_SQL, (CC_RESIDENCY_SOURCE,)).fetchone()
                conv_id = residency_latest[0] if residency_latest else None
            if conv_id is not None:
                for residency_rowid, residency_model, residency_tokens in residency_connection.execute(
                        CC_RESIDENCY_STREAM_SQL, (CC_RESIDENCY_SOURCE, conv_id)):
                    residency_turns += 1
                    try:
                        residency_count = int(residency_tokens or 0)
                        if residency_model not in residency_rates:
                            residency_rates[residency_model] = pricing.price(residency_model)["cached_in"]
                        residency_rate = residency_rates[residency_model]
                        residency_measured += residency_count * residency_rate / CC_RESIDENCY_TOKEN_SCALE
                    except (ValueError, TypeError, KeyError) as residency_error:
                        _cc_residency_logger.exception("Unreadable residency ledger unit %s", residency_rowid)
                        residency_gaps.append({"unit": f"spend_events:{residency_rowid}",
                                               "error": repr(residency_error),
                                               "traceback": traceback.format_exc()})
    except LedgerReadError:
        # Expected containment refusals retain their type for optional status surfaces.
        raise
    except Exception as residency_failure:
        _cc_residency_logger.exception("Residency ledger scan refused")
        raise LedgerReadError(
            f"Residency ledger scan refused: {residency_failure!r}\n{traceback.format_exc()}") from residency_failure
    return {"conv_id": conv_id, "turns": residency_turns, "model": residency_model,
            "measured_recurring_usd": residency_measured, "gaps": residency_gaps}


class LedgerReadError(RuntimeError):
    """A ledger consumer could not obtain a complete answer."""


class LedgerSettlementIncomplete(LedgerReadError):
    """Recorded input spend exists but the complete settlement is unknown."""

    def __init__(self, incomplete_count, input_cost_floor):
        self.incomplete_count = incomplete_count
        self.input_cost_floor = input_cost_floor
        super().__init__(
            f"Calls ledger has {incomplete_count} incomplete settlements; "
            f"known input-cost floor {input_cost_floor!r}; total spend and output tokens are unknown")


class LedgerReadTooLarge(LedgerReadError):
    """The complete matching result exceeds its explicitly named bound."""

    def __init__(self, cap, total_at_least):
        self.cap = cap
        self.total_at_least = total_at_least
        super().__init__(f"Ledger read exceeds row cap {cap}: total at least {total_at_least}; narrow filters or count")


class LedgerReadTimeout(LedgerReadError):
    """SQLite exhausted the consumer's wall-clock budget."""

    def __init__(self, budget):
        self.budget = budget
        super().__init__(f"Ledger read exceeded wall-clock budget {budget} seconds")


class LedgerQueryDeadline(LedgerReadTimeout):
    """A request-path spend aggregate could not finish within its caller deadline."""


class LedgerRequestAbandoned(LedgerReadError):
    """The serving transport abandoned its request; no complete answer exists."""


class LedgerRequestReadScope:
    """Share only interrupt handles with the transport, never SQLite ownership.

    SQLite interrupt is safe from a watcher thread. The query-owning thread
    always disposes its connection in the bounded reader's finally block.
    """

    def __init__(self, deadline_s):
        if not math.isfinite(deadline_s) or deadline_s <= 0:
            raise LedgerQueryDeadline(deadline_s)
        self.deadline_at = time.monotonic() + deadline_s
        self.deadline_s = deadline_s
        self._request_cancelled = threading.Event()
        self._request_connection_lock = threading.Lock()
        self._request_connections = set()
        self._request_cursors = weakref.WeakSet()

    def cancel_ledger_request(self):
        self._request_cancelled.set()
        with self._request_connection_lock:
            for request_connection in self._request_connections:
                request_connection.interrupt()
            pending_cursors = tuple(self._request_cursors)
        for pending_cursor in pending_cursors:
            pending_cursor._release_request_cursor_if_idle()

    def register_request_read_cursor(self, cursor):
        with self._request_connection_lock:
            self.require_ledger_request_live()
            self._request_cursors.add(cursor)

    def release_request_read_cursor(self, cursor):
        with self._request_connection_lock:
            self._request_cursors.discard(cursor)

    def require_ledger_request_live(self):
        if self._request_cancelled.is_set():
            raise LedgerRequestAbandoned("Ledger request transport abandoned the read")

    def register_request_read_connection(self, connection):
        with self._request_connection_lock:
            self.require_ledger_request_live()
            self._request_connections.add(connection)

    def release_request_read_connection(self, connection):
        with self._request_connection_lock:
            self._request_connections.discard(connection)


_LEDGER_ACTIVE_REQUEST_SCOPE = ContextVar("spendguard_ledger_request_scope", default=None)


def current_ledger_request_scope():
    """Return the serving request's cancellation owner for managed SQL reads."""
    return _LEDGER_ACTIVE_REQUEST_SCOPE.get()


def effective_ledger_admission_deadline(deadline_at=None):
    """Intersect an absolute operation expiry with the ambient request budget.

    This reads context only and takes no locks, so admission keeps its lock order.
    """
    scope = current_ledger_request_scope()
    if scope is not None:
        scope.require_ledger_request_live()
        deadline_at = scope.deadline_at if deadline_at is None else min(deadline_at, scope.deadline_at)
        if time.monotonic() >= deadline_at:
            raise LedgerQueryDeadline(scope.deadline_s)
    return deadline_at


@contextmanager
def ledger_evidence_persistence_scope():
    """Give mandatory charge recovery its own bounded, uncancelled lifetime.

    Only recovery writes may detach from a caller's expired/abandoned request.
    Optional waits and replay drains must continue to inherit that request.
    """
    persistence_scope = LedgerRequestReadScope(config.LEDGER_EVIDENCE_PERSISTENCE_BUDGET_S)
    persistence_token = _LEDGER_ACTIVE_REQUEST_SCOPE.set(persistence_scope)
    try:
        yield persistence_scope
    finally:
        persistence_scope.cancel_ledger_request()
        _LEDGER_ACTIVE_REQUEST_SCOPE.reset(persistence_token)


@contextmanager
def ledger_request_scope(*, deadline_s=None):
    """Bind request reads to one absolute deadline and cancellation owner.

    Nested dispatch retains its caller's scope; worker dispatch must propagate
    contextvars.copy_context() so cancellation crosses thread boundaries.
    """
    existing_scope = _LEDGER_ACTIVE_REQUEST_SCOPE.get()
    if existing_scope is not None:
        existing_scope.require_ledger_request_live()
        if deadline_s is None:
            yield existing_scope
            return
        if not math.isfinite(deadline_s) or deadline_s <= 0:
            raise LedgerQueryDeadline(deadline_s)
        if time.monotonic() + deadline_s >= existing_scope.deadline_at:
            yield existing_scope
            return
    scope_budget = config.LEDGER_QUERY_DEADLINE_S if deadline_s is None else deadline_s
    if scope_budget is None:
        scope_budget = config.LEDGER_READ_TIME_BUDGET_S
    request_scope = LedgerRequestReadScope(scope_budget)
    request_token = _LEDGER_ACTIVE_REQUEST_SCOPE.set(request_scope)
    try:
        yield request_scope
    finally:
        request_scope.cancel_ledger_request()
        _LEDGER_ACTIVE_REQUEST_SCOPE.reset(request_token)


def bind_ledger_query_deadline(function):
    """Start the caller's budget before setup and preserve tighter nested budgets."""
    query_signature = inspect.signature(function)
    query_has_deadline = "deadline_s" in query_signature.parameters
    query_deadline_default = query_signature.parameters["deadline_s"].default if query_has_deadline else None

    @functools.wraps(function)
    def invoke_deadline_bound_ledger_query(*args, **kwargs):
        query_arguments = query_signature.bind(*args, **kwargs)
        query_duration = query_arguments.arguments.get("deadline_s", query_deadline_default)
        if query_duration is None and current_ledger_request_scope() is None:
            return function(*args, **kwargs)
        with ledger_request_scope(deadline_s=query_duration) as query_scope:
            query_remaining = query_scope.deadline_at - time.monotonic()
            if query_remaining <= 0:
                raise LedgerQueryDeadline(query_duration)
            if query_has_deadline and query_duration is not None:
                query_arguments.arguments["deadline_s"] = query_remaining
            query_result = function(*query_arguments.args, **query_arguments.kwargs)
            query_scope.require_ledger_request_live()
            if time.monotonic() >= query_scope.deadline_at:
                raise LedgerQueryDeadline(query_duration)
            return query_result

    return invoke_deadline_bound_ledger_query


class LedgerReadSchemaError(LedgerReadError):
    """An existing ledger lacks the requested schema; reads never migrate it."""


class LedgerReadOpenError(LedgerReadSchemaError):
    """The ledger could not be opened before any consumer query ran."""


class LedgerReadTableAbsent(LedgerReadSchemaError):
    """A required table is absent in the same snapshot as the consumer read."""


@dataclass(frozen=True)
class LedgerReadRow(Mapping):
    """Immutable named values; SQLite cursor/Row objects never escape."""

    columns: tuple[str, ...]
    values: tuple[object, ...]

    def __getitem__(self, key):
        return self.values[key] if isinstance(key, int) else self.values[self.columns.index(key)]

    def __iter__(self) -> Iterator[str]:
        return iter(self.columns)

    def __len__(self):
        return len(self.columns)


@dataclass(frozen=True)
class LedgerReadRequest:
    table: str = LEDGER_EVENTS_TABLE
    columns: tuple[str, ...] = SPEND_EVENT_READ_COLUMNS
    where_sql: str = ""
    parameters: tuple = ()
    order_by: tuple[str, ...] = ()
    group_by: tuple[str, ...] = ()
    row_cap: int = field(default_factory=_default_ledger_read_row_cap)
    time_budget: float = DEFAULT_LEDGER_READ_TIME_BUDGET_S


@dataclass(frozen=True)
class LedgerSQLReadRequest:
    """Internal compatibility request for existing projected reporting queries."""

    sql: str
    parameters: tuple = ()
    row_cap: int = field(default_factory=_default_ledger_read_row_cap)
    time_budget: float = DEFAULT_LEDGER_READ_TIME_BUDGET_S
    discard_overflow_probe: bool = False
    required_tables: tuple[str, ...] = ()


@contextmanager
def _ledger_read_connection(path=None, time_budget=DEFAULT_LEDGER_READ_TIME_BUDGET_S, *, bulk_chunked=False):
    with _ledger_read_observation():
        with _ledger_bounded_read_handle(path, time_budget, bulk_chunked=bulk_chunked) as connection:
            yield connection


@contextmanager
def _ledger_bounded_read_handle(path, time_budget, *, bulk_chunked=False):
    if not math.isfinite(time_budget) or time_budget <= 0:
        raise ValueError("Ledger read time budget must be finite and positive")
    if DEFAULT_LEDGER_READ_PROGRESS_VM_INSTRUCTIONS <= 0:
        raise LedgerReadError("Ledger read progress instruction interval must be positive")
    started = time.monotonic()
    deadline_at = effective_ledger_admission_deadline(started + time_budget)
    request_scope = _LEDGER_ACTIVE_REQUEST_SCOPE.get()
    if bulk_chunked and request_scope is not None:
        raise LedgerReadError("Offline chunked reads cannot borrow a request scope")
    if request_scope is not None:
        request_scope.require_ledger_request_live()
    try:
        ledger_path = Path(path if path is not None else config.db_path()).expanduser().resolve()
        remaining = deadline_at - time.monotonic()
        if remaining <= 0:
            raise LedgerReadTimeout(time_budget)
        read_owner = config.managed_ledger_read_connection(ledger_path.as_uri() + "?mode=ro", uri=True,
            timeout=min(config.LEDGER_CONNECT_TIMEOUT_S, remaining),
            deadline_at=deadline_at)
        connection = read_owner.__enter__()
    except TimeoutError as exc:
        raise LedgerReadTimeout(time_budget) from exc
    except (sqlite3.Error, OSError) as exc:
        # SQLite's busy timeout uses integer milliseconds and can exhaust just
        # before the monotonic deadline. Its BUSY result is already a bounded
        # lock-wait refusal; a second clock sample must not change its type.
        # SQLITE_LOCKED is a different condition and keeps the ordinary error.
        if (getattr(exc, "sqlite_errorcode", 0) & LEDGER_READ_SQLITE_PRIMARY_CODE_MASK == sqlite3.SQLITE_BUSY
                or time.monotonic() - started >= time_budget):
            raise LedgerReadTimeout(time_budget) from exc
        raise LedgerReadOpenError(f"Ledger read cannot open existing database: {exc}") from exc
    expired = False

    def require_ledger_bulk_chunk_live():
        """Refuse expired offline chunks before any fetched rows are consumed."""
        if time.monotonic() >= deadline_at:
            raise LedgerReadTimeout(time_budget)

    def ledger_read_progress():
        nonlocal expired
        progress_now = time.monotonic()
        expired = progress_now >= deadline_at
        if request_scope is not None and request_scope._request_cancelled.is_set():
            return 1
        if expired:
            return int(expired)
        previous_progress_interval = read_deadline_polling.interval
        next_progress_interval = read_deadline_polling.advance_ledger_deadline_poll(progress_now)
        if next_progress_interval != previous_progress_interval:
            connection.set_progress_handler(ledger_read_progress, next_progress_interval)
        return int(expired)

    try:
        # This disposable read-only owner already supplies a whole-read deadline
        # and teardown. Shared pooled-cursor cleanup must not race its owner.
        connection._ledger_owned_read_deadline = True
        if request_scope is not None:
            request_scope.register_request_read_connection(connection)
        from .ledger import _DecSum
        connection.create_aggregate("dec_sum", LEDGER_READ_DECIMAL_ARITY, _DecSum)
        if request_scope is not None:
            request_scope.require_ledger_request_live()
        polling_started = time.monotonic()
        if polling_started >= deadline_at:
            raise LedgerReadTimeout(time_budget)
        if bulk_chunked:
            # Offline reconciliation checks natural fetch boundaries. Request
            # owners retain fine VM polling for latency and abandonment safety.
            connection._ledger_bulk_chunk_live = require_ledger_bulk_chunk_live
        else:
            read_deadline_polling = LedgerDeadlinePolling(deadline_at, polling_started)
            connection.set_progress_handler(ledger_read_progress, read_deadline_polling.interval)
        yield connection
        if request_scope is not None:
            request_scope.require_ledger_request_live()
        if time.monotonic() >= deadline_at:
            raise LedgerReadTimeout(time_budget)
    except sqlite3.Error as exc:
        if request_scope is not None:
            request_scope.require_ledger_request_live()
        if (getattr(exc, "sqlite_errorcode", 0) & LEDGER_READ_SQLITE_PRIMARY_CODE_MASK == sqlite3.SQLITE_BUSY
                or expired or time.monotonic() - started >= time_budget):
            raise LedgerReadTimeout(time_budget) from exc
        if getattr(exc, "sqlite_errorcode", None) == sqlite3.SQLITE_ERROR:
            raise LedgerReadSchemaError(f"Ledger read schema unavailable: {exc}") from exc
        raise LedgerReadError(f"Ledger read could not obtain a complete answer: {exc}") from exc
    finally:
        if request_scope is not None:
            request_scope.release_request_read_connection(connection)
        try:
            # SQLite close can defer native disposal while a failed statement's
            # cursor survives in an exception traceback. Explicitly end this
            # exclusively read-only owner's transaction before deferred close.
            connection.set_progress_handler(None, LEDGER_READ_DISABLE_PROGRESS_VM_INSTRUCTIONS)
            connection.rollback()
        finally:
            read_owner.__exit__(None, None, None)


def _read_incomplete_settlement_totals(connection, since=None, project=None):
    """Read explicit completeness metadata, retaining unknown historical state.

    This is the one raw calls read: it reports the gap rather than consuming an
    incomplete settlement as monetary or token evidence. Old schemas have no
    recoverable completeness information and are never backfilled here.
    """
    available = {column[1] for column in connection.execute("PRAGMA table_info(calls)")}
    if not all(column in available for column in INCOMPLETE_CALL_SCHEMA_COLUMNS):
        return {"n": 0, "input_cost_floor": None}
    incomplete_sql = INCOMPLETE_CALL_TOTALS_SQL
    incomplete_parameters = []
    if since is not None:
        incomplete_sql += " AND ts>=?"
        incomplete_parameters.append(since)
    if project is not None:
        from .budget import normalize_ledger_project_id
        incomplete_sql += " AND project=?"
        incomplete_parameters.append(normalize_ledger_project_id(project))
    incomplete_row = connection.execute(incomplete_sql, tuple(incomplete_parameters)).fetchone()
    return {"n": int(incomplete_row[0]), "input_cost_floor": incomplete_row[1]}


def read_incomplete_call_settlements(since=None, project=None, path=None, *,
                                    time_budget=DEFAULT_LEDGER_READ_TIME_BUDGET_S):
    """Expose selected settlement gaps and their known input-cost floor to receipts."""
    with _ledger_read_connection(path, time_budget=time_budget) as incomplete_connection:
        return _read_incomplete_settlement_totals(incomplete_connection, since, project)


@contextmanager
def _reserved_ledger_evidence_read_owner(connection):
    """Own a whole reserved report so statement cleanup cannot end its snapshot."""
    from .ledger_writer import require_ledger_writer_request_live
    evidence_scope = current_ledger_request_scope()
    prior_evidence_owner = getattr(connection, "_ledger_owned_read_deadline", False)
    prior_evidence_progress, prior_evidence_interval = getattr(connection, "_ledger_progress", (None, 0))
    evidence_polling = LedgerDeadlinePolling(evidence_scope.deadline_at, time.monotonic())
    evidence_interval = evidence_polling.interval
    if prior_evidence_progress is not None and prior_evidence_interval > 0:
        evidence_interval = min(evidence_interval, prior_evidence_interval)

    def poll_reserved_evidence_deadline():
        try:
            require_ledger_writer_request_live()
        except LedgerReadError:
            return True
        return prior_evidence_progress() if prior_evidence_progress is not None else False

    evidence_scope.register_request_read_connection(connection)
    try:
        connection._ledger_owned_read_deadline = True
        connection.set_progress_handler(poll_reserved_evidence_deadline, evidence_interval)
        try:
            require_ledger_writer_request_live()
            yield
            require_ledger_writer_request_live()
        except sqlite3.Error:
            require_ledger_writer_request_live()
            raise
    finally:
        connection.set_progress_handler(prior_evidence_progress, prior_evidence_interval)
        connection._ledger_owned_read_deadline = prior_evidence_owner
        evidence_scope.release_request_read_connection(connection)


@contextmanager
def _ledger_sql_evidence_snapshot(connection):
    """Isolate evidence and temporary views without owning a reserved writer's transaction."""
    from .ledger_writer import execute_ledger_savepoint_cleanup
    owns_evidence_transaction = not connection.in_transaction
    connection.execute("BEGIN" if owns_evidence_transaction else
                       f"SAVEPOINT {LEDGER_EVIDENCE_SNAPSHOT_SAVEPOINT}")
    try:
        yield
    finally:
        # Cleanup must work after request expiry and must never commit or roll
        # back the reserved owner's writes. SQLite rolls back TEMP DDL too.
        if connection.in_transaction:
            if owns_evidence_transaction:
                sqlite3.Connection.rollback(connection)
            else:
                execute_ledger_savepoint_cleanup(
                    connection, f"ROLLBACK TO SAVEPOINT {LEDGER_EVIDENCE_SNAPSHOT_SAVEPOINT}")
                execute_ledger_savepoint_cleanup(
                    connection, f"RELEASE SAVEPOINT {LEDGER_EVIDENCE_SNAPSHOT_SAVEPOINT}")


def read_ledger_sql(request: LedgerSQLReadRequest, path=None) -> tuple[LedgerReadRow, ...]:
    """Execute one complete bounded projected report, or raise without returning rows."""
    from .ledger_writer import reserved_ledger_bulk_read_connection
    resolved_path = path or config.db_path()
    if request.row_cap <= 0:
        raise ValueError("Ledger read row cap must be positive")
    # SQL lexical validation is parsing, not a semantic/meaning classification.
    tokens = request.sql.upper().split()
    if not tokens or tokens[0] not in ("SELECT", "WITH"):
        raise ValueError("Ledger read requests require SELECT")
    if "*" in "".join(request.sql.upper().split()).replace(LEDGER_READ_COUNT_EXPRESSION, ""):
        raise ValueError("Ledger reads require explicit column projections")
    from contextlib import nullcontext
    from .ledger_writer import require_ledger_writer_request_live
    reserved = reserved_ledger_bulk_read_connection(resolved_path)
    native_reserved = getattr(reserved, "_native_reconcile_connection", reserved)
    maintenance_live = getattr(reserved, "require_reconcile_maintenance_live", None)
    if maintenance_live is not None:
        maintenance_live()
    with (
        ledger_request_scope(deadline_s=request.time_budget) if reserved is not None else nullcontext(),
        (nullcontext(native_reserved) if reserved is not None
         else _ledger_read_connection(path, request.time_budget)) as connection,
        (_reserved_ledger_evidence_read_owner(connection) if reserved is not None else nullcontext()),
        _ledger_sql_evidence_snapshot(connection),
    ):
        if reserved is not None:
            require_ledger_writer_request_live()
        # Completeness and consumer evidence must share one SQLite snapshot;
        # a concurrent writer cannot insert an incomplete row between them.
        for required_table in request.required_tables:
            if connection.execute(
                    "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name=?",
                    (required_table,)).fetchone()[0] == 0:
                raise LedgerReadTableAbsent(f"Ledger read table absent: {required_table}")
        incomplete_totals = _read_incomplete_settlement_totals(connection)
        # Normalize explicit telemetry gaps once, before ANY consumer's SQL.
        # A TEMP view shadows only this disposable reader's calls table; it
        # neither migrates nor changes the monetary settlement axis. Legacy
        # schemas without the flag keep their existing NULL markers.
        telemetry_columns = [column[1] for column in connection.execute("PRAGMA main.table_info(calls)")]
        if "token_usage_complete" in telemetry_columns and "out_tok" in telemetry_columns:
            telemetry_projection = ["rowid AS rowid"]
            for telemetry_column in telemetry_columns:
                telemetry_identifier = '"' + telemetry_column.replace('"', '""') + '"'
                telemetry_projection.append(
                    f"CASE WHEN token_usage_complete=0 THEN NULL ELSE {telemetry_identifier} END AS {telemetry_identifier}"
                    if telemetry_column == "out_tok" else telemetry_identifier)
            connection.execute("CREATE TEMP VIEW calls AS SELECT " + ",".join(telemetry_projection) + " FROM main.calls")
        refused_incomplete_calls = False

        def authorize_complete_call_evidence(action, table, column, database, trigger):
            """Let SQLite resolve resources; never guess SQL meaning from text.

            Refuse even predicates such as cost>0 that would hide unknown costs
            before aggregation. Scope is deliberately ledger-wide: arbitrary
            SQL predicates cannot establish a safe subset without interpreting
            or rewriting the consumer's SQL contract.
            """
            nonlocal refused_incomplete_calls
            if (action == sqlite3.SQLITE_READ and table in SETTLEMENT_GUARDED_EVIDENCE_TABLES
                    and incomplete_totals["n"]):
                refused_incomplete_calls = True
                return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK

        connection.set_authorizer(authorize_complete_call_evidence)
        try:
            cursor = connection.execute(request.sql, request.parameters)
        except sqlite3.Error as incomplete_read_failure:
            if refused_incomplete_calls:
                raise LedgerSettlementIncomplete(
                    incomplete_totals["n"], incomplete_totals["input_cost_floor"]
                ) from incomplete_read_failure
            raise
        finally:
            connection.set_authorizer(None)
        columns = tuple(item[0] for item in cursor.description)
        rows = cursor.fetchmany(request.row_cap + LEDGER_READ_OVERFLOW_PROBE_ROW_COUNT)
        if request.discard_overflow_probe and len(rows) > request.row_cap:
            rows = rows[:-LEDGER_READ_OVERFLOW_PROBE_ROW_COUNT]
        if len(rows) > request.row_cap:
            raise LedgerReadTooLarge(request.row_cap, len(rows))
        result = tuple(LedgerReadRow(columns, tuple(row)) for row in rows)
        cursor.close()
        if reserved is not None:
            require_ledger_writer_request_live()
    if maintenance_live is not None:
        maintenance_live()
    return result


def read_recorded_call_total(path=None, *, time_budget=DEFAULT_LEDGER_READ_TIME_BUDGET_S):
    """Return raw SUM(calls.cost), never countable billed spend or an unknown zero.

    Missing/unreadable/locked/corrupt ledgers and missing schema raise
    LedgerReadError. Consumers may explicitly catch that and choose zero or None.
    """
    try:
        recorded_rows = read_ledger_sql(
            LedgerSQLReadRequest(RECORDED_CALL_TOTAL_SQL, time_budget=time_budget), path)
    except config.LedgerConnectionLimitError as exc:
        raise LedgerReadOpenError(f"Recorded-call total connection admission refused: {exc}") from exc
    return float(recorded_rows[0]["recorded_call_total"])


def read_conversation_spend_since(since, conv, *, deadline_s=config.LEDGER_QUERY_DEADLINE_S):
    """Thin in-process name for the existing countable conversation aggregate."""
    from . import budget
    try:
        return budget.spent_since(since, conv=conv, deadline_s=deadline_s)
    except config.LedgerConnectionLimitError as exc:
        raise LedgerReadOpenError(f"Conversation spend connection admission refused: {exc}") from exc


def read_filtered_ledger_count(table=LEDGER_EVENTS_TABLE, since=None, until=None, where=None, path=None):
    """Count every matching record without materializing it or applying a row cap."""
    if table not in LEDGER_READ_ALLOWED_TABLES:
        raise ValueError(f"Unsupported ledger table {table!r}")
    columns = SPEND_EVENT_READ_COLUMNS if table == LEDGER_EVENTS_TABLE else RECENT_CALL_READ_COLUMNS
    time_column = "day" if table == LEDGER_EVENTS_TABLE else "ts"
    predicates, parameters = [], []
    for operator, value in ((">=", since), ("<=", until)):
        if value is not None:
            predicates.append(f"{time_column} {operator} ?")
            parameters.append(value)
    for column, value in (where or {}).items():
        if column not in columns:
            raise ValueError(f"Unknown ledger count filter {column!r}")
        predicates.append(f"{column} = ?")
        parameters.append(value)
    return read_ledger_count(LedgerReadRequest(table=table, columns=columns,
        where_sql=" AND ".join(predicates), parameters=tuple(parameters)), path)


def _ledger_request_sql(request):
    if request.table not in LEDGER_READ_ALLOWED_TABLES:
        raise ValueError(f"Unsupported ledger table {request.table!r}")
    sql = f"SELECT {', '.join(request.columns)} FROM {request.table}"
    if request.where_sql:
        sql += " WHERE " + request.where_sql
    if request.group_by:
        sql += " GROUP BY " + ", ".join(request.group_by)
    if request.order_by:
        sql += " ORDER BY " + ", ".join(request.order_by)
    return sql


def read_ledger_rows(request: LedgerReadRequest, path=None) -> tuple[LedgerReadRow, ...]:
    return read_ledger_sql(LedgerSQLReadRequest(_ledger_request_sql(request), request.parameters,
                                             request.row_cap, request.time_budget), path)


def read_ledger_count(request: LedgerReadRequest | None = None, path=None) -> int:
    request = request if request is not None else LedgerReadRequest()
    sql = "SELECT COUNT(*) AS total FROM (" + _ledger_request_sql(request) + ")"
    result = read_ledger_sql(LedgerSQLReadRequest(sql, request.parameters,
                                               time_budget=request.time_budget), path)
    return int(result[0]["total"])


def read_spend_events(since=None, until=None, where=None, category_column=None, limit=None, path=None):
    from .ledger import USD_COLS
    predicates, parameters = [], []
    for column, operator, value in (("day", ">=", since), ("day", "<=", until)):
        if value is not None:
            predicates.append(f"{column} {operator} ?")
            parameters.append(value)
    for column, value in (where or {}).items():
        if column not in SPEND_EVENT_READ_COLUMNS:
            raise ValueError(f"Unknown ledger filter column {column!r}")
        predicates.append(f"{column} = ?")
        parameters.append(value)
    if category_column is not None:
        if category_column not in USD_COLS:
            raise ValueError(f"Unknown ledger category column {category_column!r}")
        predicates.append(f"{category_column} IS NOT NULL")
    request = LedgerReadRequest(where_sql=" AND ".join(predicates), parameters=tuple(parameters),
                                order_by=("day", "id"),
                                row_cap=limit if limit is not None else _default_ledger_read_row_cap())
    return read_ledger_rows(request, path)


def read_ledger_event(event_id, path=None):
    rows = read_ledger_rows(LedgerReadRequest(where_sql="id = ?", parameters=(event_id,)), path)
    return rows[0] if rows else None


def read_recent_calls(since=None, until=None, where=None, limit=None, path=None):
    predicates, parameters = [], []
    for operator, value in ((">=", since), ("<=", until)):
        if value is not None:
            predicates.append(f"ts {operator} ?")
            parameters.append(value)
    for column, value in (where or {}).items():
        if column not in RECENT_CALL_READ_COLUMNS:
            raise ValueError(f"Unknown call filter column {column!r}")
        predicates.append(f"{column} = ?")
        parameters.append(value)
    return read_ledger_rows(LedgerReadRequest(LEDGER_CALLS_TABLE, RECENT_CALL_READ_COLUMNS,
        " AND ".join(predicates), tuple(parameters), ("ts DESC", "id"),
        row_cap=limit if limit is not None else _default_ledger_read_row_cap()), path)


def read_spend_aggregates(group_by=("provider", "day"), since=None, until=None, path=None):
    from .ledger import USD_COLS
    dimensions = tuple(group_by)
    if dimensions not in LEDGER_READ_AGGREGATE_DIMENSIONS:
        raise ValueError(f"Unsupported ledger aggregate dimensions {dimensions!r}")
    predicates, parameters = [], []
    for operator, value in ((">=", since), ("<=", until)):
        if value is not None:
            predicates.append(f"day {operator} ?")
            parameters.append(value)
    projections = dimensions + tuple(f"dec_sum({column}) AS {column}" for column in USD_COLS) + ("COUNT(*) AS n",)
    return read_ledger_rows(LedgerReadRequest(columns=projections, where_sql=" AND ".join(predicates),
        parameters=tuple(parameters), group_by=dimensions, order_by=dimensions), path)


def read_advisor_evidence(as_of=None, intent=None, path=None):
    predicates = ["(intent IS NULL OR intent NOT LIKE ?)"]
    parameters = [ADVISOR_INTERNAL_INTENT_PATTERN]
    if as_of is not None:
        predicates.append("substr(ts,1,10)<=?")
        parameters.append(as_of)
    if intent is not None:
        predicates.append("intent=?")
        parameters.append(intent)
    return read_ledger_rows(LedgerReadRequest(LEDGER_CALLS_TABLE, ADVISOR_EVIDENCE_COLUMNS,
        " AND ".join(predicates), tuple(parameters)), path)


def read_invoice_overage_months(path=None):
    return read_ledger_sql(LedgerSQLReadRequest(INVOICE_OVERAGE_MONTH_SQL,
        (INVOICE_OVERAGE_SOURCE, INVOICE_OVERAGE_INTENT_PATTERN)), path)


def read_cost_integrity_buckets(since_days, path=None):
    """Expose disjoint fresh-input/read/write axes; incomplete legacy buckets retain NULL, never zero.

    Reads do not migrate old ledgers. COUNT(column) prevents SUM from hiding partially missing cache evidence.
    """
    schema = read_ledger_sql(LedgerSQLReadRequest(COST_INTEGRITY_SCHEMA_SQL, (LEDGER_CALLS_TABLE,)), path)
    available = {row["name"] for row in schema}
    projections = tuple(
        f"CASE WHEN COUNT({column})=COUNT(*) THEN SUM({column}) END" if column in available else "NULL"
        for column in COST_INTEGRITY_CACHE_COLUMNS)
    # Split calls.in_tok follows pricing's API: reads are included, writes are separate.
    # Preserve total input for incomplete legacy buckets so their best-effort ceiling stays unchanged.
    read_column = COST_INTEGRITY_CACHE_COLUMNS[0]
    complete_split = " AND ".join(f"COUNT({column})=COUNT(*)" for column in COST_INTEGRITY_CACHE_COLUMNS)
    input_projection = (f"CASE WHEN COUNT(in_tok)<>COUNT(*) THEN NULL WHEN {complete_split} "
                        f"THEN SUM(in_tok-{read_column}) ELSE SUM(in_tok) END"
                        if all(column in available for column in COST_INTEGRITY_CACHE_COLUMNS) else "SUM(in_tok)")
    completeness_group = (",(" + " AND ".join(f"{column} IS NOT NULL" for column in COST_INTEGRITY_CACHE_COLUMNS)
                          + ")" if all(column in available for column in COST_INTEGRITY_CACHE_COLUMNS) else "")
    sql = COST_INTEGRITY_BUCKET_SQL.format(input_projection=input_projection, cache_read_projection=projections[0],
                                          cache_write_projection=projections[1],
                                          cache_completeness_group=completeness_group)
    cutoff = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=int(since_days))
    return read_ledger_sql(LedgerSQLReadRequest(sql, (cutoff.isoformat(timespec="seconds"),)), path)


def read_receipt_lane_activity(since, path=None):
    return read_ledger_sql(LedgerSQLReadRequest(RECEIPT_LANE_ACTIVITY_SQL, (since,)), path)


def read_receipt_truncation_groups(since, path=None):
    return read_ledger_sql(LedgerSQLReadRequest(RECEIPT_TRUNCATION_GROUPS_SQL, (since,)), path)


def read_cc_spend_overview(path=None):
    return read_ledger_sql(LedgerSQLReadRequest(CC_SPEND_OVERVIEW_SQL), path)[0]


def read_cc_top_conversations(limit=None, path=None):
    row_cap = limit if limit is not None else _default_ledger_read_row_cap()
    return read_ledger_sql(LedgerSQLReadRequest(
        CC_TOP_CONVERSATIONS_SQL, row_cap=row_cap, discard_overflow_probe=True), path)


def read_cc_conversation_cost(conv, path=None):
    return read_ledger_sql(LedgerSQLReadRequest(CC_CONVERSATION_COST_SQL, (conv,)), path)[0]


def read_recorded_call_intents(min_calls, limit, path=None):
    return read_ledger_sql(LedgerSQLReadRequest(CALL_INTENTS_SQL, (int(min_calls),), row_cap=int(limit)), path)


def read_call_max_rowid(path=None):
    return int(read_ledger_sql(LedgerSQLReadRequest(CALL_MAX_ROWID_SQL), path)[0][0])


def read_call_flow(since_rowid=0, chain=None, path=None):
    where = "rowid > ?" + (" AND chain=?" if chain else "")
    parameters = (int(since_rowid or 0),) + ((chain,) if chain else ())
    return read_ledger_rows(LedgerReadRequest(LEDGER_CALLS_TABLE, CALL_FLOW_COLUMNS, where, parameters), path)[0]


def read_call_intent_cost(intent, since, path=None):
    return read_ledger_rows(LedgerReadRequest(LEDGER_CALLS_TABLE, READ_CALL_INTENT_COST_COLUMNS,
        "intent=? AND ts>=?", (intent, since)), path)[0][0]


def read_call_cost_summary(intent=None, path=None):
    where = "(intent IS NULL OR intent NOT LIKE ?)" + (" AND intent=?" if intent else "")
    parameters = (ADVISOR_INTERNAL_INTENT_PATTERN,) + ((intent,) if intent else ())
    return read_ledger_rows(LedgerReadRequest(LEDGER_CALLS_TABLE, CALL_COST_COLUMNS, where, parameters,
        ("SUM(cost) DESC",), ("intent", "model")), path)


def read_call_output_means(intent, call_class=None, origin_session=None, path=None):
    predicates = ["intent=?", "out_tok IS NOT NULL", "out_tok > 0", "suspect IS NULL"]
    parameters = [intent]
    for column, value in (("call_class", call_class), ("origin_session", origin_session)):
        if value is not None:
            predicates.append(f"{column}=?")
            parameters.append(value)
    columns = (CALL_EXECUTOR_EXPRESSION + " AS executor", CALL_MODEL_EXPRESSION + " AS model",
               "COALESCE(AVG(out_tok),0) AS avg_out", "COUNT(*) AS n")
    return read_ledger_rows(LedgerReadRequest(LEDGER_CALLS_TABLE, columns, " AND ".join(predicates),
        tuple(parameters), group_by=(CALL_EXECUTOR_EXPRESSION, "model")), path)


def read_call_lane_outcomes(executor, since=None, call_class=None, path=None):
    predicates, parameters = ["COALESCE(NULLIF(executor,''), provider)=?"], [executor]
    for column, operator, value in (("ts", ">=", since), ("call_class", "=", call_class)):
        if value is not None:
            predicates.append(f"{column}{operator}?")
            parameters.append(value)
    columns = (CALL_MODEL_EXPRESSION + " AS model", CALL_OUTCOME_EXPRESSION + " AS outcome", "COUNT(*) AS n", "MAX(ts) AS last_ts")
    return read_ledger_rows(LedgerReadRequest(LEDGER_CALLS_TABLE, columns, " AND ".join(predicates),
        tuple(parameters), group_by=(CALL_MODEL_EXPRESSION, CALL_OUTCOME_EXPRESSION)), path)


def read_call_metered_intents(since, path=None):
    where = "cost > 0 AND ts >= ? AND COALESCE(NULLIF(executor,''),'') IN ('','api') AND kind='realtime' AND (intent IS NULL OR intent NOT LIKE ?)"
    return read_ledger_rows(LedgerReadRequest(LEDGER_CALLS_TABLE, CALL_METERED_COLUMNS, where,
        (since, ADVISOR_INTERNAL_INTENT_PATTERN), ("SUM(cost) DESC",), ("intent", "model")), path)


def count_recent_call_tests(intent, since, kinds, model=None, path=None):
    where = "intent=? AND ts>=? AND kind IN (" + ",".join("?" for _ in kinds) + ")"
    parameters = (intent, since, *kinds)
    if model:
        where += " AND model=?"
        parameters += (model,)
    return read_ledger_count(LedgerReadRequest(LEDGER_CALLS_TABLE, COUNT_RECENT_CALL_TESTS_COLUMNS, where, parameters), path)


def read_known_call_intents(include_internal=False, path=None, include_empty=False):
    where = "intent IS NOT NULL" + (" AND intent != ''" if not include_empty else "")
    parameters = ()
    if not include_internal:
        where += " AND intent NOT LIKE ?"
        parameters = (ADVISOR_INTERNAL_INTENT_PATTERN,)
    return read_ledger_rows(LedgerReadRequest(LEDGER_CALLS_TABLE, CALL_KNOWN_INTENT_COLUMN,
        where, parameters), path)


def read_lane_subscription_calls(since, valued, path=None):
    if not valued:
        return ()
    where = "kind='subscription' AND executor IN (" + ",".join("?" for _ in valued) + ") AND ts >= ?"
    return read_ledger_rows(LedgerReadRequest(LEDGER_CALLS_TABLE, LANE_SUBSCRIPTION_READ_COLUMNS,
        where, (*sorted(valued), since)), path)


def read_lane_recent_counts(hours, path=None):
    return read_ledger_rows(LedgerReadRequest(LEDGER_CALLS_TABLE, READ_LANE_RECENT_COUNTS_COLUMNS,
        "executor IS NOT NULL AND executor NOT IN ('', 'api') AND ts >= datetime('now', ?)",
        (f"-{int(hours)} hours",), group_by=("executor",)), path)


def read_intent_model_usage(intent, path=None):
    return read_ledger_rows(LedgerReadRequest(LEDGER_CALLS_TABLE, READ_INTENT_MODEL_USAGE_COLUMNS,
        "intent=? AND model IS NOT NULL", (intent,), order_by=("n DESC",), group_by=("model",)), path)


def read_bakeoff_measurements(intent, path=None):
    return read_ledger_rows(LedgerReadRequest(LEDGER_CALLS_TABLE, READ_BAKEOFF_MEASUREMENTS_COLUMNS,
        "caller='bakeoff' AND intent IS ? AND quality IS NOT NULL", (intent,)), path)


def read_lane_fallback_costs(since=None, path=None):
    where = "fell_from IS NOT NULL AND fell_from != ''" + (" AND ts >= ?" if since else "")
    return read_ledger_rows(LedgerReadRequest(LEDGER_CALLS_TABLE,
        READ_LANE_FALLBACK_COSTS_COLUMNS, where,
        (str(since),) if since else (), order_by=("cost DESC",), group_by=("fell_from",)), path)


def read_lane_token_metrics(lane, path=None):
    return read_ledger_rows(LedgerReadRequest(LEDGER_CALLS_TABLE, LANE_TOKEN_METRIC_COLUMNS,
        "executor=?", (lane,)), path)[0]


def read_lane_cumulative_tokens(path=None):
    return read_ledger_rows(LedgerReadRequest(LEDGER_CALLS_TABLE,
        READ_LANE_CUMULATIVE_TOKENS_COLUMNS,
        "executor IS NOT NULL AND executor NOT IN ('','api')", group_by=("executor",)), path)


def count_lane_prompts(lane, window_days, path=None):
    return read_ledger_count(LedgerReadRequest(LEDGER_CALLS_TABLE, COUNT_RECENT_CALL_TESTS_COLUMNS,
        "executor=? AND ts>=datetime('now',?)", (lane, f"-{float(window_days)} days")), path)


def read_prompt_lint_evidence(intent=None, since=None, path=None):
    predicates, parameters = ["intent IS NOT NULL"], []
    for column, operator, value in (("intent", "=", intent), ("ts", ">=", since)):
        if value:
            predicates.append(f"{column}{operator}?")
            parameters.append(value)
    return read_ledger_rows(LedgerReadRequest(LEDGER_CALLS_TABLE, PROMPT_LINT_READ_COLUMNS,
        " AND ".join(predicates), tuple(parameters)), path)


def read_delegate_overage_evidence(path=None):
    invoices = read_ledger_sql(LedgerSQLReadRequest(DELEGATE_INVOICED_MONTHS_SQL,
        (INVOICE_OVERAGE_SOURCE, INVOICE_OVERAGE_INTENT_PATTERN)), path)
    months = tuple(row["month"] for row in invoices if float(row["dollars"] or 0) > 0)
    if not months:
        return invoices, None
    placeholders = ",".join("?" for _ in months)
    value = read_ledger_rows(LedgerReadRequest(LEDGER_EVENTS_TABLE,
        READ_DELEGATE_OVERAGE_EVIDENCE_COLUMNS,
        f"source=? AND substr(occurred_at,1,7) IN ({placeholders})",
        (DELEGATE_PLAN_SOURCE, *months)), path)[0]["value"]
    return invoices, value




def _route_realized_where_clause(intent, since=None, as_of=None):
    predicates = ["intent=?"]
    args = [intent]
    if since is not None:
        predicates.append("ts>=?")
        args.append(since)
    if as_of is not None:
        predicates.append("ts<=?")
        args.append(as_of)
    return " AND ".join(predicates), args


def _fetch_route_realized_rows(sql, args):
    return tuple(row.values for row in read_ledger_sql(LedgerSQLReadRequest(sql, tuple(args))))


def read_realized_adoptions(intent=None, path=None):
    predicates = "intent IS NOT NULL AND (status IS NULL OR status != 'refuted')"
    if intent:
        predicates += " AND intent=?"
    return read_ledger_rows(LedgerReadRequest(LEDGER_INSIGHTS_TABLE, READ_REALIZED_ADOPTIONS_COLUMNS,
        predicates, (intent,) if intent else (), group_by=("intent",)), path)


def read_realized_call_rates(intent, adopted_ts, path=None):
    return read_ledger_rows(LedgerReadRequest(LEDGER_CALLS_TABLE,
        READ_REALIZED_CALL_RATES_COLUMNS,
        "intent=? AND cost>0", (adopted_ts, adopted_ts, adopted_ts, adopted_ts, intent)), path)[0]


def ledger_read_table_exists(table, path=None):
    return bool(read_ledger_sql(LedgerSQLReadRequest(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,)), path))


def read_recording_gap_cells(intent_like=None, path=None):
    where = ("intent IS NOT NULL AND cost>0 AND suspect IS NULL "
             "AND (COALESCE(in_tok,0)+COALESCE(out_tok,0))>0")
    if intent_like:
        where += " AND intent LIKE ?"
    return read_ledger_rows(LedgerReadRequest(LEDGER_CALLS_TABLE,
        READ_RECORDING_GAP_CELLS_COLUMNS,
        where, (f"%{intent_like}%",) if intent_like else (),
        group_by=("intent", "provider", "model", "date(ts)")), path)


def read_represented_call_cell(intent, model, day, path=None):
    from .ledger import LLM_USD_COLS, SpendLedger
    represented = (f"COALESCE(SUM(CASE WHEN {SpendLedger._COUNTABLE} THEN "
                   + "+".join(f"COALESCE({column},0)" for column in LLM_USD_COLS)
                   + " ELSE 0 END),0) AS represented")
    return read_ledger_rows(LedgerReadRequest(LEDGER_EVENTS_TABLE, (represented,),
        "intent=? AND model=? AND day=?", (intent, model, day)), path)[0][0]


def read_routing_history_report(intent, *, since=None, as_of=None, limit=20):
    """Aggregate exact stored routing outcomes for one intent and optional ``ts`` window."""
    where, args = _route_realized_where_clause(intent, since, as_of)
    limit = max(0, int(limit))
    totals_row = _fetch_route_realized_rows(
        f"""SELECT COUNT(*),
                   COALESCE(SUM(CASE WHEN {LEDGER_ROUTING_METERED_SQL} THEN cost ELSE 0 END),0),
                   COALESCE(SUM(CASE WHEN {LEDGER_ROUTING_METERED_SQL} THEN 1 ELSE 0 END),0),
                   COALESCE(SUM(CASE WHEN COALESCE(cost,0)<=0 THEN 1 ELSE 0 END),0),
                   COALESCE(SUM(CASE WHEN {LEDGER_ROUTING_EXECUTOR_SQL}='unknown' OR {LEDGER_ROUTING_LANE_SQL}='unknown' THEN 1 ELSE 0 END),0),
                   COALESCE(SUM(CASE WHEN cost>0 THEN 1 ELSE 0 END),0),
                   COALESCE(SUM(CASE WHEN cost>0 THEN cost ELSE 0 END),0),
                   COALESCE(SUM(CASE WHEN COALESCE(cost,0)<=0
                                     AND {LEDGER_ROUTING_EXECUTOR_SQL} NOT IN ('unknown','api') THEN 1 ELSE 0 END),0)
            FROM calls WHERE {where}""", args)[0]
    (jobs, billed_cost, billed_jobs, zero_jobs, unknown_jobs, positive_cost_jobs,
     positive_cost, zero_lane_jobs) = totals_row
    totals = {"jobs": int(jobs), "billed_cost": float(billed_cost or 0),
              "billed_jobs": int(billed_jobs), "zero_cost_jobs": int(zero_jobs),
              "unknown_routing_jobs": int(unknown_jobs)}
    if int(positive_cost_jobs) != int(billed_jobs):
        totals["positive_cost_jobs"] = int(positive_cost_jobs)
        totals["positive_cost"] = float(positive_cost)

    grouped = _fetch_route_realized_rows(
        f"""SELECT {LEDGER_ROUTING_EXECUTOR_SQL}, {LEDGER_ROUTING_LANE_SQL}, COUNT(*),
                   COALESCE(SUM(CASE WHEN {LEDGER_ROUTING_METERED_SQL} THEN cost ELSE 0 END),0),
                   COALESCE(SUM(CASE WHEN {LEDGER_ROUTING_METERED_SQL} THEN 1 ELSE 0 END),0),
                   COALESCE(SUM(CASE WHEN COALESCE(cost,0)<=0 THEN 1 ELSE 0 END),0),
                   GROUP_CONCAT(DISTINCT NULLIF(served_model,'')),
                   GROUP_CONCAT(DISTINCT NULLIF(requested_model,''))
            FROM calls WHERE {where}
            GROUP BY {LEDGER_ROUTING_EXECUTOR_SQL}, {LEDGER_ROUTING_LANE_SQL}
            ORDER BY COUNT(*) DESC, {LEDGER_ROUTING_EXECUTOR_SQL}, {LEDGER_ROUTING_LANE_SQL}""", args)
    by_executor_lane = [
        {"executor": ex, "resolved_lane": lane, "jobs": int(n), "billed_cost": float(cost or 0),
         "billed_jobs": int(billed), "zero_cost_jobs": int(zero),
         "served_models": sorted(models.split(",")) if models else [],
         "requested_models": sorted(requested.split(",")) if requested else []}
        for ex, lane, n, cost, billed, zero, models, requested in grouped
    ]

    model_rows = _fetch_route_realized_rows(
        f"""SELECT {LEDGER_ROUTING_SERVED_SQL}, COUNT(*),
                   COALESCE(SUM(CASE WHEN {LEDGER_ROUTING_METERED_SQL} THEN cost ELSE 0 END),0),
                   COALESCE(SUM(CASE WHEN {LEDGER_ROUTING_METERED_SQL} THEN 1 ELSE 0 END),0),
                   COALESCE(SUM(CASE WHEN COALESCE(cost,0)<=0 THEN 1 ELSE 0 END),0)
            FROM calls WHERE {where} GROUP BY {LEDGER_ROUTING_SERVED_SQL}
            ORDER BY COUNT(*) DESC, {LEDGER_ROUTING_SERVED_SQL}""", args)
    by_served_model = [
        {"served_model": model, "jobs": int(n), "billed_cost": float(cost or 0),
         "billed_jobs": int(billed), "zero_cost_jobs": int(zero)}
        for model, n, cost, billed, zero in model_rows
    ]

    drift_rows = _fetch_route_realized_rows(
        f"""SELECT requested_model, served_model, COUNT(*) FROM calls WHERE {where}
            AND NULLIF(requested_model,'') IS NOT NULL AND NULLIF(served_model,'') IS NOT NULL
            AND requested_model != served_model
            GROUP BY requested_model, served_model ORDER BY COUNT(*) DESC, requested_model, served_model""", args)
    model_drift = [{"requested_model": req, "served_model": served, "jobs": int(n)}
                   for req, served, n in drift_rows]

    fell_where = f"{where} AND (NULLIF(fell_from,'') IS NOT NULL OR NULLIF(fell_reason,'') IS NOT NULL)"
    total_fell = _fetch_route_realized_rows(f"SELECT COUNT(*) FROM calls WHERE {fell_where}", args)[0][0]
    fell_reason_rows = _fetch_route_realized_rows(
        f"""SELECT COALESCE(NULLIF(fell_reason,''),'unknown'), COUNT(*),
                   COALESCE(SUM(CASE WHEN {LEDGER_ROUTING_METERED_SQL} THEN cost ELSE 0 END),0)
            FROM calls WHERE {fell_where}
            GROUP BY COALESCE(NULLIF(fell_reason,''),'unknown') ORDER BY COUNT(*) DESC, 1""", args)
    fell_from_rows = _fetch_route_realized_rows(
        f"""SELECT COALESCE(NULLIF(fell_from,''),'unknown'), COUNT(*) FROM calls WHERE {fell_where}
            GROUP BY COALESCE(NULLIF(fell_from,''),'unknown') ORDER BY COUNT(*) DESC, 1""", args)
    redirect_rows = _fetch_route_realized_rows(
        f"""SELECT COALESCE(NULLIF(redirect_reason,''),'unknown'), COUNT(*) FROM calls WHERE {fell_where}
            GROUP BY COALESCE(NULLIF(redirect_reason,''),'unknown') ORDER BY COUNT(*) DESC, 1""", args)
    fell = {
        "total_fell_jobs": int(total_fell),
        "by_fell_reason": [{"fell_reason": reason, "jobs": int(n), "billed_cost": float(cost or 0)}
                           for reason, n, cost in fell_reason_rows],
        "by_fell_from": [{"fell_from": lane, "jobs": int(n)} for lane, n in fell_from_rows],
        "by_redirect_reason": [{"redirect_reason": reason, "jobs": int(n)} for reason, n in redirect_rows],
    }

    # The bounded reader sees the complete projection before the report selects
    # its explicitly labelled display samples; SQL never hides an oversized set.
    sample_rows = _fetch_route_realized_rows(
        f"""SELECT ts, {LEDGER_ROUTING_EXECUTOR_SQL}, {LEDGER_ROUTING_LANE_SQL}, requested_model, served_model, cost,
                   redirect_reason, fell_reason FROM calls WHERE {where}
            ORDER BY ts DESC""", args) if limit else ()
    samples = [dict(zip(("ts", "executor", "resolved_lane", "requested_model", "served_model", "cost",
                         "redirect_reason", "fell_reason"), row)) for row in sample_rows[:limit]]

    caveats = []
    if unknown_jobs:
        caveats.append(f"{int(unknown_jobs)} rows have NULL/empty executor or resolved_lane (legacy) → bucketed as 'unknown'")
    if not jobs:
        caveats.append("no calls found for this intent in the requested ts window")
    if int(positive_cost_jobs) != int(billed_jobs):
        caveats.append("positive-cost rows outside the metered executor predicate are counted separately as positive_cost_jobs")
    shares = {"metered_share_jobs": (int(billed_jobs) / int(jobs)) if jobs else None,
              "metered_share_cost": (float(billed_cost) / float(positive_cost)) if positive_cost else (0.0 if jobs else None),
              "zero_lane_share_jobs": (int(zero_lane_jobs) / int(jobs)) if jobs else None,
              "unknown_share_jobs": (int(unknown_jobs) / int(jobs)) if jobs else None}
    return {"intent": intent, "window": {"since": since, "as_of": as_of, "field": "ts"}, "totals": totals,
            "by_executor_lane": by_executor_lane, "by_served_model": by_served_model,
            "model_drift": model_drift, "fell": fell, "shares": shares, "samples": samples, "caveats": caveats}



def read_calibration_calls(meta_pattern, as_of=None, backtest=False, distinct_cells=False, path=None):
    where = CALIBRATION_CORPUS_PREDICATE
    where += " AND intent IS NOT NULL AND intent NOT LIKE ?" if backtest or distinct_cells else " AND (intent IS NULL OR intent NOT LIKE ?)"
    parameters = (meta_pattern,)
    if backtest:
        where += " AND kind IN ('batch','realtime')"
    if as_of:
        where += " AND ts<=?"
        parameters += (as_of,)
    columns = ("DISTINCT intent", "model", "kind") if distinct_cells else CALIBRATION_CALL_COLUMNS
    return read_ledger_rows(LedgerReadRequest(LEDGER_CALLS_TABLE, columns, where, parameters), path)


def read_calibration_actuals(chain=None, intent=None, since=None, until=None, path=None):
    if chain is not None:
        where, parameters = "chain=?", (chain,)
    else:
        where, parameters = "intent=? AND ts>=? AND ts<=?", (intent, since, until)
    return read_ledger_rows(LedgerReadRequest(LEDGER_CALLS_TABLE, CALIBRATION_ACTUAL_COLUMNS, where, parameters), path)


def read_calibration_summary(meta_pattern, label=None, path=None):
    where = CALIBRATION_CORPUS_PREDICATE + " AND intent IS NOT NULL AND intent NOT LIKE ?"
    parameters = (meta_pattern,)
    if label:
        where += " AND intent=?"
        parameters += (label,)
    return read_ledger_rows(LedgerReadRequest(LEDGER_CALLS_TABLE,
        READ_CALIBRATION_SUMMARY_COLUMNS, where,
        parameters, order_by=("cost DESC",), group_by=("intent", "model", "kind")), path)




def read_cc_overflow_conversations(path=None):
    return read_ledger_rows(LedgerReadRequest(LEDGER_EVENTS_TABLE, CC_OVERFLOW_COLUMNS,
        "source='claude-code-overflow'", group_by=("conv_id",)), path)


def read_cc_attribution_evidence(windows=(), path=None):
    real = read_ledger_rows(LedgerReadRequest(LEDGER_EVENTS_TABLE, CC_ATTRIBUTION_MONTH_COLUMNS,
        "source='anthropic-invoice' AND intent LIKE 'anthropic-invoice:cc-overage%'", group_by=("mo",)), path)
    window_predicates = []
    window_parameters = []
    for window_start, window_end in windows:
        window_predicates.append("(occurred_at>=? AND occurred_at<?)")
        window_parameters.extend((datetime.datetime.fromtimestamp(window_start, datetime.timezone.utc).isoformat(),
                                  datetime.datetime.fromtimestamp(window_end, datetime.timezone.utc).isoformat()))
    evidence = ()
    if window_predicates:
        evidence = read_ledger_rows(LedgerReadRequest(
            LEDGER_EVENTS_TABLE, CC_ATTRIBUTION_WINDOW_COLUMNS,
            CC_ESTIMATE_PREDICATE + " AND (" + " OR ".join(window_predicates) + ")",
            tuple(window_parameters), group_by=("mo", "conv_id")), path)
    fallback = read_ledger_rows(LedgerReadRequest(LEDGER_EVENTS_TABLE, CC_ATTRIBUTION_FALLBACK_COLUMNS,
        CC_ESTIMATE_PREDICATE, group_by=("mo", "conv_id")), path)
    return real, evidence, fallback


def read_cc_conversation_turns(min_day=None, path=None):
    where, parameters = "source='claude-code'", ()
    if min_day:
        where += " AND occurred_at>=?"
        parameters = (min_day,)
    return read_ledger_rows(LedgerReadRequest(LEDGER_EVENTS_TABLE, CC_CONVERSATION_TURN_COLUMNS,
        where, parameters, order_by=("conv_id", "occurred_at")), path)

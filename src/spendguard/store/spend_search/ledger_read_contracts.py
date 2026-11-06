"""Dependency-free identity contracts for bounded ledger reads."""

from dataclasses import dataclass, field


LEDGER_EVENTS_TABLE = "spend_events"
LEDGER_CALLS_TABLE = "calls"
LEDGER_INSIGHTS_TABLE = "insights"
LEDGER_READ_DECIMAL_ARITY = 1
LEDGER_READ_SQLITE_PRIMARY_CODE_MASK = 0xFF
SPEND_EVENT_READ_COLUMNS = tuple("""id dedup_key source content_hash schema_version ts_utc occurred_at recorded_at tz local_datetime day period eligibility_window window_start window_end currency batch_usd realtime_usd est_chat_usd remote_compute_usd subscription_usd external_usd cost_type billed is_meta cost_basis amount_confidence rate_in rate_out cache_read_rate cache_write_rate fx_rate base_usd provider model model_kind finish in_tok out_tok cache_read_tok cache_write_tok reasoning_tok num_calls num_items latency org team projects project_primary member_ref key_fp account_id customer_id cost_center engagement billable invoice_id conv_id seg_id call_id cwd batch_id from_message_ids prior_message_ids post_message_ids script repo host prompt_hash prompt_snip output_snip evidence_uri intent actor chain attr_what attr_why attr_how attr_reason attr_confidence attr_source attr_model attr_ts attr_version recorded_by ingest_version status revision locked locked_at lock_reason reverses_id adjusts_id superseded_by reconciled reconciled_vs reconciled_at reconciliation_id gap_flag period_closed recon_marker quality quality_src quality_conf cache_hit savings_cv tags""".split())

_ledger_read_row_cap_default_resolver = None
_ledger_read_time_budget_default_resolver = None


def set_ledger_read_request_default_resolvers(row_cap_resolver, time_budget_resolver):
    """Wire live configuration defaults without importing the configuration domain."""
    global _ledger_read_row_cap_default_resolver, _ledger_read_time_budget_default_resolver
    _ledger_read_row_cap_default_resolver = row_cap_resolver
    _ledger_read_time_budget_default_resolver = time_budget_resolver


def _default_ledger_read_row_cap():
    if _ledger_read_row_cap_default_resolver is None:
        raise RuntimeError("import spendguard before constructing a ledger read request")
    return _ledger_read_row_cap_default_resolver()


def _default_ledger_read_time_budget():
    if _ledger_read_time_budget_default_resolver is None:
        raise RuntimeError("import spendguard before constructing a ledger read request")
    return _ledger_read_time_budget_default_resolver()


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


class LedgerReadSchemaError(LedgerReadError):
    """An existing ledger lacks the requested schema; reads never migrate it."""


class LedgerReadOpenError(LedgerReadSchemaError):
    """The ledger could not be opened before any consumer query ran."""


class LedgerReadTableAbsent(LedgerReadSchemaError):
    """A required table is absent in the same snapshot as the consumer read."""


@dataclass(frozen=True)
class LedgerReadRequest:
    table: str = LEDGER_EVENTS_TABLE
    columns: tuple[str, ...] = SPEND_EVENT_READ_COLUMNS
    where_sql: str = ""
    parameters: tuple = ()
    order_by: tuple[str, ...] = ()
    group_by: tuple[str, ...] = ()
    row_cap: int = field(default_factory=_default_ledger_read_row_cap)
    time_budget: float = field(default_factory=_default_ledger_read_time_budget)


@dataclass(frozen=True)
class LedgerSQLReadRequest:
    """Internal compatibility request for existing projected reporting queries."""

    sql: str
    parameters: tuple = ()
    row_cap: int = field(default_factory=_default_ledger_read_row_cap)
    time_budget: float = field(default_factory=_default_ledger_read_time_budget)
    discard_overflow_probe: bool = False
    required_tables: tuple[str, ...] = ()

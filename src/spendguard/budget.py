"""Cross-process spend ledger (SQLite, WAL) for fleet-wide DAILY / MONTHLY caps — no proxy.

Enabled by config `budget.backend = sqlite`. The gate records every charge here and checks cumulative
spend across ALL processes before allowing more. Default `backend = memory` keeps the per-process
real-time cap only (this module is then never touched). Per-call SQLite I/O is fine for moderate
real-time volume; very high-volume loops should stay on the in-process cap.
"""
import contextlib, datetime, hashlib, pathlib, tempfile, threading
import sys
import time
import functools
import inspect
import math
from numbers import Number
from decimal import Decimal
from . import config
from .ledger_fork_locks import new_fork_reset_ledger_state
from .ledger_writer import serialize_ledger_function


def require_finite_budget_value(value):
    """Reject incomplete monetary arithmetic instead of manufacturing a cap."""
    finite = value.is_finite() if isinstance(value, Decimal) else (
        math.isfinite(value) if isinstance(value, Number) else True)
    if not finite:
        raise ValueError("Budget projection requires finite monetary values")
    if isinstance(value, (tuple, list)):
        for component in value:
            require_finite_budget_value(component)
    return value


def finite_budget_projection(project):
    @functools.wraps(project)
    def enforce_finite_budget_projection(*args, **kwargs):
        for value in (*args, *kwargs.values()):
            require_finite_budget_value(value)
        return require_finite_budget_value(project(*args, **kwargs))
    return enforce_finite_budget_projection


def _budget_snapshot_generation_observer():
    """Keep SQLite's connection-relative commit counter on a read-only owner."""
    from .ledger_read import _consumer_ledger_read_target, effective_ledger_admission_deadline
    path = pathlib.Path(config.db_path()).resolve()
    read_target = _consumer_ledger_read_target(path)
    # Empty-install compatibility owns a transient database until the real ledger
    # materializes. Its commit counter cannot observe that different database.
    observer_identity = (path, read_target)
    observer = getattr(_LEDGER_TL, "projection_observer", None)
    if observer is not None and observer[0] != observer_identity:
        observer[1].__exit__(None, None, None)
        observer = None
    if observer is None:
        owner = config.managed_ledger_read_connection(read_target, uri=True,
            deadline_at=effective_ledger_admission_deadline(
                time.monotonic() + config.LEDGER_QUERY_DEADLINE_S))
        connection = owner.__enter__()
        observer = (observer_identity, owner, connection)
        _LEDGER_TL.projection_observer = observer
    return observer[2], observer[2].execute("PRAGMA data_version").fetchone()[0]


@contextlib.contextmanager
def budget_projection_reader(*, deadline_s=None):
    """Use committed WAL snapshots, except reserved bulk scopes read pending."""
    from .ledger import SpendLedger
    from .ledger_writer import current_ledger_writer_connection, ledger_connection_commit_deferred
    from .ledger_read import (_ledger_read_connection, consumer_empty_spend_compatibility,
                              SPEND_EVENT_READ_COLUMNS, DEFAULT_LEDGER_READ_TIME_BUDGET_S,
                              LedgerReadTimeout, LedgerQueryDeadline)
    time_budget = DEFAULT_LEDGER_READ_TIME_BUDGET_S if deadline_s is None else deadline_s
    if not math.isfinite(time_budget) or time_budget <= 0:
        raise LedgerQueryDeadline(time_budget)
    existing = getattr(_LEDGER_TL, "read_ledger", None)
    if existing is not None:
        yield existing
        return
    owner = current_ledger_writer_connection(config.db_path())
    cached = getattr(_LEDGER_TL, "led", None)
    if (owner is None and cached is not None
            and cached.db_path == config.db_path()):
        owner = cached._conn
    if owner is not None and ledger_connection_commit_deferred(owner):
        pending_reader = object.__new__(SpendLedger)
        pending_reader.db_path = config.db_path()
        pending_reader._conn = owner
        pending_reader._cols = set(SPEND_EVENT_READ_COLUMNS)
        pending_reader._explicit_owned_connection = True
        pending_reader._total_memo = None
        pending_reader._budget_projection_connection_owned = True
        yield pending_reader
        return
    try:
        with consumer_empty_spend_compatibility(), _ledger_read_connection(
                time_budget=time_budget) as connection:
            snapshot_reader = object.__new__(SpendLedger)
            snapshot_reader.db_path = config.db_path()
            snapshot_reader._conn = connection
            snapshot_reader._cols = set(SPEND_EVENT_READ_COLUMNS)
            snapshot_reader._explicit_owned_connection = True
            snapshot_reader._total_memo = None
            snapshot_reader._budget_projection_connection_owned = True
            snapshot_reader._budget_projection_snapshot = True
            snapshot_reader._budget_projection_generation_observer = _budget_snapshot_generation_observer
            # Coverage and money observe one complete committed version.
            connection.execute("BEGIN")
            snapshot_reader._total_memo = getattr(cached, "_total_memo", None)
            yield snapshot_reader
            if cached is not None:
                cached._total_memo = snapshot_reader._total_memo
    except LedgerReadTimeout as failure:
        raise LedgerQueryDeadline(time_budget) from failure


def budget_projection_snapshot(read_projection):
    projection_signature = inspect.signature(read_projection)

    @functools.wraps(read_projection)
    def read_budget_projection_snapshot(*args, **kwargs):
        arguments = projection_signature.bind(*args, **kwargs)
        arguments.apply_defaults()
        with budget_projection_reader(deadline_s=arguments.arguments.get("deadline_s")) as reader:
            previous = getattr(_LEDGER_TL, "read_ledger", None)
            _LEDGER_TL.read_ledger = reader
            try:
                if (getattr(reader, "_budget_projection_snapshot", False)
                        and "deadline_s" in projection_signature.parameters):
                    arguments.arguments["deadline_s"] = None
                    args, kwargs = arguments.args, arguments.kwargs
                return require_finite_budget_value(read_projection(*args, **kwargs))
            finally:
                _LEDGER_TL.read_ledger = previous
    return read_budget_projection_snapshot

from .ledger_read import bind_ledger_query_deadline

_conn = None
BUDGET_AUXILIARY_POOL_KEY = "budget_auxiliary_reads"
_lock = new_fork_reset_ledger_state(globals(), "_lock", threading.RLock)
_SPEND_EVENT_DEADLETTER_FILENAME = "spend_events_deadletter.jsonl"
_SPEND_EVENT_EMERGENCY_SPOOL_PREFIX = "spendguard-capture-gap-"
# Visible attribution fallback for events with no explicit project or captured venv.
UNATTRIBUTED_PROJECT = "unattributed"
LIVE_GATE_SOURCE = "gate"
def spend_event_retry_paths():
    """Ordered durable retry spools; the OS temp fallback survives an unwritable spendguard home."""
    home_key = hashlib.sha256(str(config.HOME).encode()).hexdigest()
    return (config.HOME / _SPEND_EVENT_DEADLETTER_FILENAME,
            pathlib.Path(tempfile.gettempdir()) /
            f"{_SPEND_EVENT_EMERGENCY_SPOOL_PREFIX}{home_key}.jsonl")


def _ensure_budget_auxiliary_read_schema(connection):
    """Auxiliary tables are initialized by their serialized writer owners."""


def _ledger_db():
    """Borrow the writer owner inside mutations, or acquire a bounded per-thread auxiliary read handle.
    It NO LONGER creates the retired `charges` table: the money-of-record is `spend_events` (owned by
    SpendLedger), every reader/writer here goes through it, and a fresh install has no `charges` to make.
    The one-time `migrate_charges` bridge reads a PRE-EXISTING charges via its own connection; it never needs
    this to create one, and the migration tests build their own charges fixture."""
    from .ledger_writer import current_ledger_writer_connection
    owner = current_ledger_writer_connection()
    if owner is not None:
        return owner
    return config.pooled_ledger_conn(BUDGET_AUXILIARY_POOL_KEY, _ensure_budget_auxiliary_read_schema)


_PROJECT = None


# The ONE definition of "money spent in a period" now lives on the money-of-record as SpendLedger._COUNTABLE
# (exclude meta / reconciliation markers / quarantined-impossible / reconstructed; an estimate still binds a
# cap). It replaced the countable_charges VIEW over the retired `charges` table — same rule, one place.


_LEDGER_TL = threading.local()


def _ledger():
    """Cache a connection-free facade; reads close scoped handles and mutations borrow the serialized owner."""
    scoped_reader = getattr(_LEDGER_TL, "read_ledger", None)
    if scoped_reader is not None:
        return scoped_reader
    led = getattr(_LEDGER_TL, "led", None)
    if led is None:
        from .ledger import SpendLedger
        led = _LEDGER_TL.led = SpendLedger()
    from .ledger_writer import current_ledger_writer_connection
    owner_connection = current_ledger_writer_connection()
    if owner_connection is not None:
        import copy
        led = copy.copy(led)
        led._conn = owner_connection
    return led


def _budget_writer_is_initialized():
    """An existing facade or executing owner can read without schema initialization."""
    from .ledger_writer import current_ledger_writer_connection
    if current_ledger_writer_connection(config.db_path()) is not None:
        return True
    initialized_ledger = getattr(_LEDGER_TL, "led", None)
    return (initialized_ledger is not None
            and pathlib.Path(initialized_ledger.db_path).resolve() == pathlib.Path(config.db_path()).resolve())


def _consumer_aggregate_snapshot(aggregate):
    """Keep pure consumer aggregates off schema initialization and writer admission."""
    deadline_parameter = inspect.signature(aggregate).parameters.get("deadline_s")

    @functools.wraps(aggregate)
    def invoke_consumer_budget_aggregate(*args, **kwargs):
        if (_budget_writer_is_initialized()
                or getattr(_LEDGER_TL, "read_ledger", None) is not None):
            return aggregate(*args, **kwargs)
        from .ledger import SpendLedger
        from .ledger_read import (_ledger_read_connection, DEFAULT_LEDGER_READ_TIME_BUDGET_S,
                                  SPEND_EVENT_READ_COLUMNS, LedgerReadTimeout, LedgerQueryDeadline,
                                  consumer_empty_spend_compatibility)
        aggregate_budget = (kwargs.get("deadline_s", deadline_parameter.default)
                            if deadline_parameter is not None else DEFAULT_LEDGER_READ_TIME_BUDGET_S)
        if aggregate_budget is None:
            aggregate_budget = DEFAULT_LEDGER_READ_TIME_BUDGET_S
        try:
            with consumer_empty_spend_compatibility(), \
                    _ledger_read_connection(time_budget=aggregate_budget) as aggregate_connection:
                aggregate_reader = object.__new__(SpendLedger)
                aggregate_reader.db_path = config.db_path()
                aggregate_reader._conn = aggregate_connection
                aggregate_reader._cols = set(SPEND_EVENT_READ_COLUMNS)
                aggregate_reader._explicit_owned_connection = True
                aggregate_reader._total_memo = None
                aggregate_reader._defer = False
                _LEDGER_TL.read_ledger = aggregate_reader
                try:
                    return aggregate(*args, **kwargs)
                finally:
                    _LEDGER_TL.read_ledger = None
        except LedgerReadTimeout as aggregate_timeout:
            raise LedgerQueryDeadline(aggregate_budget) from aggregate_timeout
    return invoke_consumer_budget_aggregate


def _reset_ledger():
    """Drop THIS thread's cached ledger connection so the next read reconnects clean — call after a destructive
    schema change. Replaces the old `budget._LEDGER = None`, which a thread-local cache cannot honor."""
    _LEDGER_TL.led = None
    _LEDGER_TL.read_ledger = None
    observer = getattr(_LEDGER_TL, "projection_observer", None)
    if observer is not None:
        observer[1].__exit__(None, None, None)
        _LEDGER_TL.projection_observer = None


def _reset_budget_after_fork():
    """A child of os.fork() must NOT reuse the parent's sqlite connections. sqlite forbids sharing a connection
    across processes — the fd and its POSIX locks belong to the parent — so a child that inherits `_conn` (the
    shared module connection) or the parent's thread-local ledger gets `database is locked`, silent corruption, or
    a double-commit of the parent's pending write. Drop both so the child reconnects fresh on next use. Registered
    at fork below; a no-op on platforms without os.register_at_fork."""
    global _conn
    _conn = None
    try:
        _reset_ledger()
    except Exception:
        pass


try:
    import os as _os
    _os.register_at_fork(after_in_child=_reset_budget_after_fork)
except (AttributeError, ValueError, ImportError):   # register_at_fork is POSIX-only — elsewhere this is a no-op
    pass


def normalize_ledger_project_id(project):
    """Use the stored project identifier's orthographic convention for reads and writes."""
    return str(project).strip().lower() if project is not None else ""


def _attribute(ev, project):
    """Stamp org/team/project on a spend_event from the charge's project tag via the prior repo→org map — a
    cheap cached dict lookup, NOT an LLM (agentic refinement is the later attribute/reconcile pass, never the
    hot path). Keeps the same attribution the charges ledger carried, in the typed columns."""
    proj = normalize_ledger_project_id(project)
    if not proj:
        proj = UNATTRIBUTED_PROJECT
    try:
        from . import conv as _conv_mod
        org, team = _conv_mod._prior_org_team(proj) if proj else ("", "")
    except Exception:
        org, team = "", ""
    ev["project_primary"] = proj
    ev["projects"] = [proj] if proj else []
    ev["org"], ev["team"] = org, team


def _record_spend_event(provider, model, kind, cost, *, conv_id="", basis="", intent="", actor="", key_fp="",
                        chain="", project="", occurred_at=None, in_tok=0, out_tok=0, cache_read_tok=0,
                        cache_write_tok=0, reasoning_tok=0, source="gate", dedup_suffix="", invoice_id="", dedup_key=None,
                        maintenance_ledger=None, posting_metadata=None):
    """THE write for a live charge → `spend_events`, the single money-of-record, through the ONE shared mapping
    (charge_to_event). Every budget writer records through here; there is no second ledger, and — since the
    cutover — no `charges` fallback behind it, so a dropped write is a dropped charge. The ledger connection
    carries sqlite's own busy_timeout (see SpendLedger, `timeout=`), so an ordinary transient lock blocks briefly
    and clears rather than failing. A failed primary write must durably spool the complete evidence under an
    independent recovery budget; if both destinations fail, raise rather than silently lose the charge.
    `chain` is the caller's JOB/RUN tag — carried onto
    the money row so billed $ slices by job, not just intent (two concurrent runs of one intent get distinct chains)."""
    try:
        from .ledger import live_dedup_key
        ev = charge_to_event(provider, model, kind, cost, conv_id=conv_id, basis=basis,
                             intent=intent, actor=actor, key_fp=key_fp, chain=chain)
        # Gate-only interpreter provenance. Captured as a LOCAL and handed to the ledger writer as an explicit
        # argument — never set as a key of `ev`, so the username-bearing path never enters the diagnostic reads
        # of `ev` (stderr capture-gap log, rate-provenance warning). record_event writes it straight to the column.
        running_venv = sys.prefix if source == LIVE_GATE_SOURCE else None
        _attribute(ev, project)
        oa = occurred_at or datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")
        ev["occurred_at"] = ev["ts_utc"] = oa
        if posting_metadata is not None:
            for posting_field in ("recorded_at", "tz", "local_datetime"):
                ev[posting_field] = posting_metadata[posting_field]
        ev["in_tok"] = None if in_tok is None else int(in_tok)
        ev["out_tok"] = None if out_tok is None else int(out_tok)
        # Cache/reasoning token axes — set ONLY when nonzero so the gate's hot path (which passes none) is byte-for-byte
        # unchanged. Claude Code re-reads the whole context every turn, so cache_read dominates; storing it per-turn as
        # its OWN column is what the context-trajectory + compaction signal reads (context = in + cache_read + cache_write).
        if cache_read_tok:
            ev["cache_read_tok"] = int(cache_read_tok)
        if cache_write_tok:
            ev["cache_write_tok"] = int(cache_write_tok)
        if reasoning_tok:
            ev["reasoning_tok"] = int(reasoning_tok)
        # A live charge gets a per-call UNIQUE key (two identical calls must NOT merge). An idempotent IMPORT
        # (OTel/reconstruction) passes an explicit STABLE dedup_key so re-ingesting the same source never double-counts.
        ev["dedup_key"] = dedup_key or live_dedup_key(oa + dedup_suffix)
        ev["source"] = ev["recorded_by"] = source
        if invoice_id:                                # FOCUS InvoiceId anchor — set only when reconciled to a bill
            ev["invoice_id"] = invoice_id
        (maintenance_ledger if maintenance_ledger is not None else _ledger()).record_event(ev, venv=running_venv)
    except Exception as e:
        if maintenance_ledger is not None:
            # The offline owner rolls back this entire correction rebuild on every write failure.
            raise
        # spend_events is the SOLE ledger now, so a failed write means this charge is genuinely absent — NOT
        # sitting safe in `charges` (dropped) and NOT rebuildable by `spendguard migrate` (which read charges).
        # DURABLY CAPTURE IT so the loss never depends on this stderr being seen: a headless/daemon consumer
        # discards stderr, and provider-truth reconcile recovers BATCH spend but cannot re-book a realtime charge
        # without an admin key. The dead-letter file is the honest record of exactly what the ledger dropped.
        retry_path, retry_error = None, None
        payload = {"provider": provider, "model": model, "kind": kind, "cost": str(cost),
                   "in_tok": None if in_tok is None else int(in_tok),
                   "out_tok": None if out_tok is None else int(out_tok), "occurred_at": occurred_at,
                   "intent": intent, "project": project, "source": source,
                   "error": repr(e), "capture_gap": True}
        if "ev" in locals():
            payload["event"] = ev
            payload["venv"] = running_venv
        for candidate in spend_event_retry_paths():
            try:
                from .ledger_deadletter import append_spend_deadletter_event
                append_spend_deadletter_event(candidate, payload)
                retry_path = candidate
                break
            except Exception as spool_error:
                retry_error = spool_error
        import sys as _sys
        marker = f"queued for retry at {retry_path}" if retry_path else f"RETRY MARKER FAILED: {retry_error!r}"
        _sys.stderr.write(f"[budget] CAPTURE GAP: spend_events write failed for {provider}/{model} ${cost}: "
                          f"{e!r}; {marker}. This charge is MISSING from the ledger; `spendguard reconcile` "
                          f"can re-book provider-truth spend.\n")
        if retry_path is None:
            raise RuntimeError("spend capture and durable retry both failed") from retry_error


def _project():
    """Project tag for a charge (the repo/work this spend belongs to) — cached per process. Order:
    $SPENDGUARD_PROJECT → saas config `project` (repo-local .spendguard.json) → git repo root basename → cwd."""
    global _PROJECT
    if _PROJECT is not None:
        return _PROJECT
    import os
    v = os.environ.get("SPENDGUARD_PROJECT")
    if not v:
        try:
            v = config.saas_config().get("project")
        except Exception:
            v = None
    if not v:
        try:
            import subprocess
            root = subprocess.run(["git", "rev-parse", "--show-toplevel"], capture_output=True, text=True, timeout=2).stdout.strip()
            if root:
                v = os.path.basename(root)
        except Exception:
            pass
    if not v:
        try:
            v = os.path.basename(os.getcwd())
        except Exception:
            v = ""
    _PROJECT = (v or "").strip().lower()[:64]
    return _PROJECT


_CONV = None


def _conv():
    """Conversation/chat id this charge belongs to — links a call back to the chat that spawned it (so per-call
    + pre/post conversation context is recoverable). Order: $SPENDGUARD_CONV / $SPENDGUARD_CHAT /
    $CLAUDE_SESSION_ID, else a stable per-process id (calls in one run share it). Cached per process."""
    global _CONV
    if _CONV is not None:
        return _CONV
    import os
    v = (os.environ.get("SPENDGUARD_CONV") or os.environ.get("SPENDGUARD_CHAT")
         or os.environ.get("CLAUDE_SESSION_ID") or "")
    if not v:
        import uuid
        v = "proc-" + uuid.uuid4().hex[:12]
    _CONV = v.strip()[:128]
    return _CONV


_reading = threading.local()


@contextlib.contextmanager
def reading_history(what=""):
    """Inside this block, the gate is READING already-billed work — it is not spending.

    WHY THIS EXISTS. Recovering a past batch means downloading its input file and parsing it. Those bytes
    look exactly like a batch about to be submitted, so the gate estimated them as new spend: one read-only,
    zero-token `callio.fetch_history()` pass wrote $359.63 of charges that never happened (plus $10,050 more
    the impossibility rail caught), and the daily cap then refused every genuine call for the rest of the day.
    A guard that blocks real work because of money nobody spent is worse than no guard — it teaches you to
    turn it off.

    This is NOT a spend bypass. It suppresses recording only for callers that are provably reading history
    (the callio fetch paths), the HTTP GETs it covers are free, and any real call made inside a block would
    still be metered by the provider — it just would not be double-counted here as a projection."""
    prev = getattr(_reading, "on", False)
    _reading.on = True
    try:
        yield
    finally:
        _reading.on = prev


def is_reading_history():
    return bool(getattr(_reading, "on", False))


def record_charge(provider, model, kind, cost, project=None, conv_id=None, basis=None,
           intent=None, actor=None, in_tok=None, out_tok=None):
    """Write one charge. `basis` says WHAT KIND of number it is (estimate · billed · assumed · reconstructed) —
    known for certain by the writer, unknowable by the reader, and the thing that makes a displayed figure
    honest. Blank on legacy rows, which read as 'unlabelled' rather than being silently called billed.

    `intent` and `actor` are the FORENSIC pair, captured from the live context when not passed:
        intent   WHAT the money bought      'review:config.py', 'spendguard:cache-test'
        actor    WHAT RAN IT                'repo_review_panel.py:fan_out:238'
    Both are read here rather than required from callers, because the caller that forgets is exactly the
    one whose spend later needs explaining. They are captured at the moment of the charge — reconstructing
    them afterwards from timestamps is guesswork, and this file exists to stop guesswork about money."""
    if not cost:
        return
    if is_reading_history():
        # Re-reading a batch that already ran is not a new charge. Writing one here inflates today's total
        # with money nobody spent, and the caps then act on the fiction.
        return
    proj = project if project is not None else _project()
    conv = conv_id if conv_id is not None else _conv()
    try:
        fp = config.key_fingerprint(provider)          # which key served this call (env-resolved proxy; ''=unknown)
    except Exception:
        fp = ""
    if intent is None or actor is None:
        try:
            from . import calls as _c
            intent = intent if intent is not None else (_c.current().get("intent") or "")
            actor = actor if actor is not None else (_c.caller() or "")
        except Exception:
            intent, actor = intent or "", actor or ""
    try:                                               # the JOB/RUN tag from the live context (spendguard.context(chain=…)):
        from . import calls as _cc                     # carried onto the money row so "my spend" slices by JOB, not just
        chain = _cc.current().get("chain") or ""       # intent (which two concurrent runs of one intent would collide on)
    except Exception:
        chain = ""
    now = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")
    _record_spend_event(provider, model, kind, float(cost), conv_id=conv or "", basis=basis or "",
                        intent=(intent or "")[:120], actor=(actor or "")[:120], key_fp=fp, chain=chain,
                        project=proj or "", occurred_at=now, source="gate", in_tok=in_tok, out_tok=out_tok)


def reconcile_money_batch_estimate(provider, model, intent, in_tok, out_tok, cost):
    """Auditably revise the newest open gate estimate to the measured batch actual; never delete or clamp rows."""
    from .ledger_writer import current_ledger_writer_connection, run_ledger_write
    if current_ledger_writer_connection() is None:
        return run_ledger_write(lambda: reconcile_money_batch_estimate(provider, model, intent, in_tok, out_tok, cost))
    led = _ledger()
    with _lock:
        row = led._conn.execute(
            "SELECT id FROM spend_events WHERE source='gate' AND cost_type='batch' AND cost_basis='estimate' "
            "AND provider=? AND model=? AND intent IS ? ORDER BY rowid DESC LIMIT 1",
            (provider, model, intent or "")).fetchone()
        if not row:
            return False
        led.update(row[0], {"batch_usd": str(cost), "in_tok": None if in_tok is None else int(in_tok),
                            "out_tok": None if out_tok is None else int(out_tok),
                            "cost_basis": "billed", "status": "reconciled"},
                   actor="batch_collect", reason="provider-measured usage collected", pass_="collect")
    return True


def snapshot(reason="", keep=None, force=False, *, compact_source=None, compact_target=None):
    """Copy the ledger database aside BEFORE anything mutates it. Returns the path, or None.

    WHY THIS EXISTS. On 2026-08-10 a guard test overwrote ~/.spendguard/config.json — 9KB of settings
    replaced by a 26-byte probe — and there was no backup to restore from: the machine's off-site job
    mirrors ~/.claude only, and this directory holds the ledger itself (43,488 rows, $24,515 recorded at
    the time). Every re-attribution and quarantine that day ran against data with no recovery path.

    A mutation without a recovery path is not a change, it is a gamble. sqlite's own backup API is used so
    a snapshot taken while another process is mid-write is still consistent — a torn copy would be a backup
    that only fails when you need it.

    `keep` bounds the LOCAL copies. These are RECOVERY snapshots, and the DEEP history lives off-machine in B2
    (spendguard-full, pushed daily), so a large local keep is pure disk bloat: measured 2026-09-15, keep=20 × a
    ~650MB ledger = 12GB of full-db copies in ~/.spendguard/snapshots. Local only needs the window since the last
    daily B2 push, so the default is small and configurable (safety.snapshot_keep, default 2 ≈ a day or two of
    recovery points). None → read that config; an explicit keep still wins."""
    from .disk_guard import require_disk_space
    from .gate import SpendGateRefused

    # Offline reclaim extends this copy boundary, but MUST NOT reuse/prune a
    # snapshot or swallow an error. The caller owns locking and verification.
    if compact_source is not None or compact_target is not None:
        if compact_source is None or compact_target is None:
            raise ValueError("compact snapshot requires both source handle and target")
        require_disk_space(compact_target)
        compact_source.execute("VACUUM INTO ?", (str(compact_target),))
        return str(compact_target)
    import datetime as _dt
    if keep is None:
        try:
            keep = max(1, int(config._cfg_get("safety", "snapshot_keep", 2)))
        except Exception:
            keep = 2
    import sqlite3 as _sq
    import time as _time
    try:
        src = config.db_path()
        from .distribution import mark_distribution_ephemeral_dir
        d = mark_distribution_ephemeral_dir(config.HOME / "snapshots")
        # THROTTLE — a snapshot is a FULL-DB copy. Before this, the scheduled reconcile's reattribute took one on
        # EVERY run, so a full backup landed ~every 45 min and keep=4 rotated through in ~3h (measured 2026-10-08:
        # the snapshot set fully regenerated between two glances). A routine mutation only needs a RECENT recovery
        # point, not a fresh full copy each time — so reuse the newest snapshot when it is younger than
        # safety.snapshot_min_interval_hours (default 24 → one per day). force=True (the destructive DELETE ops via
        # snapshot_once) always takes a fresh one; the invariant 'a mutation has a backup to restore from' holds
        # either way. The authoritative DEEP history is the daily consistent LIVE-DB copy the B2 job pushes.
        if not force:
            try:
                _recent = max((f.stat().st_mtime for f in d.glob("spend-*.db")), default=0.0)
                _min_h = float(config._cfg_get("safety", "snapshot_min_interval_hours", 24))
                if _recent and (_time.time() - _recent) < max(0.0, _min_h) * 3600.0:
                    _newest = max(d.glob("spend-*.db"), key=lambda f: f.stat().st_mtime)
                    return str(_newest)                  # a recent snapshot already protects this mutation — reuse it
            except Exception:
                pass
        stamp = _dt.datetime.now(_dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        tag = "".join(ch if ch.isalnum() or ch in "-_" else "-" for ch in (reason or "pre-mutation"))[:40]
        dst = d / f"spend-{stamp}-{tag}.db"
        require_disk_space(dst)
        with _sq.connect(src) as _s, _sq.connect(str(dst)) as _t:
            _s.backup(_t)                        # consistent even under a concurrent writer
        # Keep the most recent `keep`; a snapshot directory that grows without bound gets deleted by hand
        # one day, which is the same as having none. The pruned copies are the deep history B2 already holds.
        # Remove each snapshot's WAL/SHM companions too — globbing only spend-*.db left those orphaned.
        old = sorted(d.glob("spend-*.db"))[:-keep] if keep else []
        for f in old:
            for g in (f, f.parent / (f.name + "-wal"), f.parent / (f.name + "-shm")):
                try:
                    g.unlink()
                except OSError:
                    pass
        return str(dst)
    except SpendGateRefused:
        raise
    except Exception as e:
        import sys as _sys
        _sys.stderr.write(f"[budget] WARN could not snapshot the ledger before mutating it "
                          f"({type(e).__name__}: {str(e)[:80]}) — the change is NOT protected.\n")
        return None


@serialize_ledger_function
def reattribute_providers(apply=False, actor="reattribute_providers"):
    """Find (and optionally correct) charges whose `provider` disagrees with the model registry.

    WHY THIS IS A FUNCTION AND NOT A ONE-OFF SCRIPT. The gate inferred a charge's vendor with
    `"anthropic" if model.startswith("claude") else "openai"`, so every OpenAI-COMPATIBLE vendor was
    recorded as OpenAI: measured, 695 rows and $30.26 of Moonshot and z.ai spend sat on the OpenAI line.
    `saas reconcile` compares the ledger to provider billing PER PROVIDER, so one line was over-attributed
    by exactly what the others were missing, and the leak verdict drawn from them was wrong twice over.
    The inference is fixed, but a ledger that cannot repair its own history is not an accounting record —
    and the same class of drift will happen again the next time a vendor is added.

    DRY BY DEFAULT. Returns what it WOULD change; `apply=True` writes. Every correction is journalled to
    spend_audit with the old and new value, because a silent restatement is indistinguishable from the
    error it corrects — and in a forensic tool the correction has to be as visible as the mistake."""
    from . import gate
    from .ledger import to_dec, USD_COLS
    led = _ledger()
    rows = [r for r in led.query() if r.get("model")]     # every spend_events row carrying a model
    fixes = []
    for r in rows:
        prov, model = r.get("provider"), r.get("model")
        true = gate._provider_of(model)
        # UNKNOWN IS NOT A CORRECTION. Overwriting a recorded vendor with "unknown" destroys information;
        # only a POSITIVE identification that disagrees is a fix.
        if true and true != gate.UNKNOWN_PROVIDER and prov and true != prov:
            fixes.append({"id": r["id"], "model": model, "was": prov, "now": true,
                          "cost": float(sum((to_dec(r.get(c)) for c in USD_COLS), to_dec(0)))})
    # A KEY FINGERPRINT THAT BELONGS TO THE WRONG VENDOR IS WORSE THAN NONE. These rows were stamped with
    # the fingerprint of the provider they were MIS-labelled as, so after the vendor is corrected the row
    # reads "moonshot spend, served by an OpenAI key" — a contradiction that looks like a finding. We cannot
    # recover which key really served a past call, so it becomes EMPTY, which this column already defines as
    # unknown. Restoring the current key's fingerprint instead would assert that today's key served a call
    # made before it existed.
    if fixes:
        _wrong_fp = {}
        for f in fixes:
            try:
                _wrong_fp.setdefault(f["was"], config.key_fingerprint(f["was"]))
            except Exception:
                _wrong_fp.setdefault(f["was"], "")
    unjournalled = []
    by_id = {r["id"]: r for r in rows}
    if apply and fixes:
        snapshot(reason=f"reattribute-{actor}")     # whole-file backup before rewriting money rows
        for f in fixes:
            changes = {"provider": f["now"]}
            stale = _wrong_fp.get(f["was"]) or ""
            if stale and by_id.get(f["id"], {}).get("key_fp") == stale:
                changes["key_fp"] = ""              # a fp stamped for the mis-labelled vendor is worse than none
            # led.update writes the correction AND its audit row through the hash chain, on the ledger's own
            # connection — no separate raw INSERT (the source of 697 unchained rows), no cross-connection
            # deadlock. A locked period is refused; that row is reported unjournalled rather than silently skipped.
            try:
                led.update(f["id"], changes, actor=actor, pass_="reattribute",
                           reason=f"model {f['model']} is served by {f['now']}, not {f['was']} "
                                  f"(gate inferred the vendor from the model name prefix)")
            except Exception as e:
                unjournalled.append({"id": f["id"], "error": f"{type(e).__name__}: {str(e)[:60]}"})
        if unjournalled:
            import sys as _sys
            _sys.stderr.write(f"[budget] WARN {len(unjournalled)} of {len(fixes)} re-attributions NOT applied "
                              f"(locked period?) — e.g. {unjournalled[0]}\n")
    # SECOND PASS: a fingerprint that belongs to a DIFFERENT vendor than the row's.
    #
    # Separate from the vendor fix above because it outlives it: rows corrected in an EARLIER run still
    # carry the fingerprint they were stamped with while mis-labelled, so a row reads "moonshot spend,
    # served by an OpenAI key". Checking the whole table each time makes the repair idempotent and catches
    # rows this run did not touch. Which key really served a past call is not recoverable, so it becomes
    # EMPTY — the value this column already defines as unknown. Writing today's key instead would assert
    # that it served a call made before it existed.
    known = {}
    try:
        from . import adapters
        for v in adapters.PROVIDERS:
            fp = config.key_fingerprint(v)
            if fp:
                known[fp] = v
    except Exception:
        known = {}
    stale = []
    if known:
        for r in led.query():
            fp = r.get("key_fp")
            owner = known.get(fp) if fp else None
            if owner and r.get("provider") and owner != r["provider"]:
                stale.append({"id": r["id"], "provider": r["provider"], "key_fp": fp, "belongs_to": owner})
        if apply and stale:
            for st in stale:                        # led.update writes the correction + its chained audit row
                try:
                    led.update(st["id"], {"key_fp": ""}, actor=actor, pass_="reattribute",
                               reason=f"fingerprint belongs to {st['belongs_to']}, not {st['provider']} — the key "
                                      f"that served this call is not recoverable, so it is UNKNOWN")
                except Exception:
                    pass
    return {"n": len(fixes), "usd": round(sum(f["cost"] for f in fixes), 4), "applied": bool(apply),
            "by_move": _count_moves(fixes), "fixes": fixes[:20], "unjournalled": unjournalled,
            "stale_key_fp": len(stale)}


def _count_moves(fixes):
    out = {}
    for f in fixes:
        k = f"{f['was']}->{f['now']}"
        out[k] = {"rows": out.get(k, {}).get("rows", 0) + 1,
                  "usd": round(out.get(k, {}).get("usd", 0.0) + f["cost"], 4)}
    return out


@_consumer_aggregate_snapshot
def projects_for_conv(conv):
    """Distinct repos (projects) THIS conversation touched — its workload spend. Powers the contextual receipt's
    collapsed view (a conversation can span repos; this very chat touched llm-spendguard + manga2anime + lmm)."""
    if not conv:
        return []
    return _ledger().distinct("project_primary", where={"conv_id": str(conv), "is_meta": 0})


@_consumer_aggregate_snapshot
def all_projects():
    """All repos with workload spend (the expanded all-repos view) — excludes meta + reconciliation-mirror rows."""
    return _ledger().distinct("project_primary", where={"is_meta": 0, "reconciled": 0})


_SNAPPED = set()


def snapshot_once(reason):
    """Snapshot the ledger before the first destructive write of a given KIND in this process.

    snapshot() has existed since the config.json incident and was called from exactly ONE place, while SIX
    functions issue a DELETE against the ledger. That is the same shape as `keep_backups=0`: the capability
    was built, made opt-in, and then not opted into — so the protection existed everywhere in principle and
    nowhere in practice. Deduped per reason so a reconcile that clears rows for forty days takes one
    snapshot, not forty."""
    import os as _os
    import sys as _sys
    if reason in _SNAPPED:
        return None
    _SNAPPED.add(reason)
    try:
        p = snapshot(reason=reason, force=True)      # a destructive DELETE always gets a FRESH pre-op snapshot (not throttled)
    except Exception as e:
        p, err = None, f"{type(e).__name__}: {str(e)[:80]}"
    else:
        err = "" if p else "snapshot() returned no path"
    if p:
        _sys.stderr.write(f"  ledger snapshot before {reason}: {p}\n")
        return p
    # FAIL CLOSED. The first cut of this printed a note and let the DELETE proceed, which makes the whole
    # thing decorative: the caller believes a recovery path exists precisely when it does not, and that is
    # the same "protection that exists in principle and nowhere in practice" this function was written to
    # end. A destructive write with no backup is not a change, it is a gamble — so it does not happen unless
    # somebody says so out loud.
    if _os.environ.get("SPENDGUARD_ALLOW_UNSNAPSHOTTED") == "1":
        _sys.stderr.write(f"  ⚠ proceeding with {reason} WITHOUT a ledger snapshot ({err}) — "
                          f"SPENDGUARD_ALLOW_UNSNAPSHOTTED=1 was set.\n")
        return None
    raise RuntimeError(
        f"refusing {reason}: the ledger could not be snapshotted first ({err}). This deletes money rows and "
        f"there would be no way back. Fix the snapshot target (disk space / permissions on "
        f"{config.HOME}), or set SPENDGUARD_ALLOW_UNSNAPSHOTTED=1 to proceed without one.")


def ingest_remote(label, project, rows):
    """Roll a REMOTE box's realtime spend into the local ledger, IDEMPOTENTLY. Deletes any prior rows for this box
    (conv_id='remote:<label>') then inserts the current ones — so re-syncing a box REPLACES, never double-counts
    (a box's captioning runs on a real API key → actual-$ billed, attributed to its project). Returns (n, total)."""
    snapshot_once("ingest-remote")           # DELETEs money rows — never without a recovery path
    conv = "remote:" + str(label)
    proj = (project or "").strip().lower()
    n, total = 0, 0.0
    # REPLACE into spend_events (the money-of-record): drop this box's prior rows, then re-book the current ones,
    # so re-syncing a box never double-counts. remote_dec / by_key (this box's only readers) read spend_events.
    _ledger().delete(where={"conv_id": conv}, actor="ingest_remote", reason="re-sync remote box (replace)")
    for i, r in enumerate(rows or []):
        cost = float(r.get("cost") or 0)
        if not cost:
            continue
        day = r.get("day") or datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")
        _record_spend_event(r.get("provider") or "?", r.get("model") or "?", "remote", cost, conv_id=conv,
                            project=proj, occurred_at=day + "T00:00:00+00:00", source="remote",
                            dedup_suffix=":%s:%d" % (conv, i))
        n += 1; total += cost
    return n, total


_RECONCILED = "(provider-batch)"   # marker model for reconciliation rows (the provider-truth gap), any project
# Realtime reconcile marker models (ledger_sync's _RT_MARKER/_RT_ORACLE_MARKER/_RT_RECON_MARKER — a drift guard in
# tests/test_true_down.py asserts the two stay identical). These rows MIRROR truth from another source (gate log /
# admin oracle / conversational reconstruction), so exclude_reconciled must drop them along with _RECONCILED — the
# trust check compares "what the gate recorded live" against those very sources, and counting a mirror row on the
# recorded side double-counts it (the $13.74 realtime drift bug).
_RT_MARKERS = ("(realtime-history)", "(realtime-oracle)", "(realtime-reconstructed)")
_MARKER_MODELS = (_RECONCILED,) + _RT_MARKERS
# True-down correction rows carry the REAL model (so by_dims NETS them against the estimate rows per
# (day,provider,model,kind,project) — the server ingest clamps negative cost, so a standalone negative marker-model
# row would be dropped there) and mark themselves via conv_id instead.
_TRUE_DOWN_CONV = "(true-down)"
# An estimate we KNOW is impossible (input > the model's context window — see gate._implausible_estimate).
# It is recorded, never deleted: a forensic record of what the estimator claimed. But it is quarantined out
# of every spend total, because a number that cannot describe a real request must not read as money spent.
QUARANTINE_CONV = "(impossible-estimate)"
# UNPRICED — a call we could not price. It is the MIRROR of quarantine: quarantine holds money that cannot be
# real, this holds real usage whose price is unknown. Both must stay out of the total, and both must be SHOWN,
# because $0 and "we don't know" are different claims and only one of them is true here.
UNPRICED_CONV = "(unpriced)"

# BASIS — what KIND of number a row is. Every displayed figure needs this, or the reader has to infer it from
# the row's shape and gets it wrong: "the receipt reads as $12,000" was exactly that. Not a confidence score
# and not a judgement — it is a fact about where the number came from, known at write time by the writer.
BASIS_ESTIMATE = "estimate"          # pre-spend projection (batch ceilings live here until true-down)
BASIS_BILLED = "billed"              # the provider's own number, or usage the provider returned
BASIS_ASSUMED = "assumed"            # a default nobody confirmed (e.g. the subscription fee)
BASIS_RECONSTRUCTED = "reconstructed"  # derived after the fact from transcripts/logs, not from a bill
BASIS_UNPRICED = "unpriced"          # the call happened and the TOKENS are real; the $ is unknown, NOT zero
BASES = (BASIS_ESTIMATE, BASIS_BILLED, BASIS_ASSUMED, BASIS_RECONSTRUCTED, BASIS_UNPRICED)


# charge `kind` → (spend_events money-kind, is_meta). meta is a FLAG on a realtime row, not its own money
# column. THE one mapping — used by the live gate write AND the bulk migration, so the two cannot drift.
_KIND_TO_EVENT = {"realtime": ("realtime", 0), "batch": ("batch", 0), "meta": ("realtime", 1),
                  "remote": ("remote", 0), "est_chat": ("est_chat", 0),
                  # subscription (the flat plan fee) + est_chat (plan-covered usage value) each have their OWN money
                  # column and are valid kinds in record_event / _KIND_TO_USD; without these two entries a
                  # subscription charge fell through to ('realtime', 0) and a flat plan fee was silently
                  # misclassified as metered API spend. Lane rows are $0 so existing rows are unaffected.
                  "subscription": ("subscription", 0), "sub": ("subscription", 0),
                  # external: non-token cost from an MCP/tool call or an external paid API — a REAL $ axis of its own
                  # (BILLED_USD_COLS), kept apart from LLM token spend, GPU/remote, and the subscription fee.
                  "external": ("external", 0), "tool": ("external", 0), "mcp": ("external", 0)}


def charge_to_event(provider, model, kind, cost, conv_id="", basis="", intent="", actor="", key_fp="", chain=""):
    """THE single charge → spend_event field mapping, faithful and in ONE place so the live gate write and the
    one-time migration can never disagree about what a charge MEANS. Returns the money/role/basis fields of a
    spend_event; the caller adds attribution (org/team/project), identity (dedup_key), and provenance (source).

    The role of a charge (meta / reconciliation / impossible-quarantine / unpriced / true-down) is derived here
    from the same signals the legacy `charges` ledger used — kind, a marker model, a sentinel conv_id, the
    basis — and mapped onto the typed spend_events representation:
      • meta            → is_meta=1 (realtime money column)          (excluded from workload totals)
      • reconciliation  → reconciled=1 + recon_marker=<model>        (mirror of provider/gate truth)
      • quarantine      → status='void'                              (impossible estimate; kept, not counted)
      • unpriced        → cost_basis='unpriced', NO money column     ($ unknown ≠ $0; a forensic marker)
      • true-down       → a negative money value, status posted      (nets the estimate down)
    cost_basis carries the WHAT-KIND-OF-NUMBER axis (the charge's own `basis`), never the role — blank stays
    blank ('unlabelled'), never silently promoted to billed."""
    rec_kind, is_meta = _KIND_TO_EVENT.get((kind or "realtime").lower(), ("realtime", 0))
    reconciled = 1 if model in _MARKER_MODELS else 0
    is_quarantine = (conv_id == QUARANTINE_CONV)
    cbasis = (basis or "").strip().lower()
    is_unpriced = (not cost) and (conv_id == UNPRICED_CONV or cbasis == BASIS_UNPRICED)
    if is_unpriced:
        cost_basis = BASIS_UNPRICED
    elif cbasis in BASES:
        cost_basis = cbasis
    else:
        cost_basis = ""
    ev = {
        "provider": provider or "?", "model": model or "?",
        "conv_id": conv_id or "", "key_fp": key_fp or "",
        "intent": (intent or "")[:120], "actor": (actor or "")[:120], "chain": (chain or "")[:120],
        "is_meta": is_meta, "reconciled": reconciled,
        "recon_marker": model if reconciled else None,
        "status": "void" if is_quarantine else ("reconciled" if reconciled else "posted"),
        # est_chat is subscription-COVERED usage VALUE — no $ out the door, so it is NOT billed (it stays out of every
        # real-$ total via BILLED_USD_COLS AND via this flag). Every other kind is real money → billed.
        "cost_basis": cost_basis, "billed": 0 if rec_kind == "est_chat" else 1,
    }
    if is_unpriced:
        ev["usd"] = None                                   # price unknown → no money column; a $0 forensic marker
    else:
        ev["kind"] = rec_kind
        ev["usd"] = cost                                   # real cost (incl. negative true-down corrections)
    return ev


def require_complete_metered_settlements(since=None, project=None, *, time_budget=None, connection=None):
    """Refuse monetary totals while a recorded call's complete settlement is unknown.

    This leaves authoritative invoice events untouched. Calls without recoverable
    completeness metadata cannot be guessed; an absent ledger has no recorded gap.
    """
    if not pathlib.Path(config.db_path()).exists():
        return
    from .ledger_read import (read_incomplete_call_settlements, LedgerSettlementIncomplete,
                              _read_incomplete_settlement_totals)
    if connection is not None:
        coverage = _read_incomplete_settlement_totals(connection, since, project)
    else:
        coverage_options = {} if time_budget is None else {"time_budget": time_budget}
        coverage = read_incomplete_call_settlements(since=since, project=project, **coverage_options)
    if coverage["n"]:
        raise LedgerSettlementIncomplete(coverage["n"], coverage["input_cost_floor"])


def _require_budget_settlement_within_deadline(started, deadline_s, since=None, project=None):
    from .ledger_read import LedgerReadTimeout, LedgerQueryDeadline, DEFAULT_LEDGER_READ_TIME_BUDGET_S
    remaining = (deadline_s - (time.monotonic() - started) if deadline_s is not None
                 else DEFAULT_LEDGER_READ_TIME_BUDGET_S)
    if remaining <= 0:
        raise LedgerQueryDeadline(deadline_s)
    try:
        projection_reader = getattr(_LEDGER_TL, "read_ledger", None)
        require_complete_metered_settlements(
            since, project, time_budget=remaining,
            connection=projection_reader._conn if projection_reader is not None else None)
    except LedgerReadTimeout as coverage_timeout:
        raise LedgerQueryDeadline(deadline_s) from coverage_timeout


@bind_ledger_query_deadline
@budget_projection_snapshot
def spent_since(day, project=None, conv=None, *, deadline_s=config.LEDGER_QUERY_DEADLINE_S, read_only=False):
    """Gate-recorded workload $ since `day` — the LLM cap's number, now read from spend_events via the single
    `_COUNTABLE` filter (batch+realtime, excluding meta / reconciliation / voided-impossible / reconstructed).
    `deadline_s` optionally bounds this aggregate by a relative wall-clock duration; expiry raises
    LedgerQueryDeadline rather than returning an incomplete spend value.
    Optionally SCOPE to a `project` (repo) and/or `conv` (conversation) — the receipt uses this to show what is
    relevant to the current repo/conversation, not a global sum.
    A cold consumer automatically uses a bounded read-only snapshot, without
    initializing or migrating the writer. Every projection uses the same
    committed snapshot seam; reserved deferred bulk scopes retain pending reads.
    ``read_only`` remains accepted for compatibility with snapshot consumers.

    Repointed onto spend_events (the money-of-record). This is LLM-only by construction: GPU/remote spend has
    its OWN cap (resources.compute_exceeded) and is no longer lumped into the LLM total as the old
    countable_charges view did (charges carried no remote rows, so the live number is unchanged)."""
    spend_started = time.monotonic()
    where = {}
    if project is not None:
        where["project_primary"] = normalize_ledger_project_id(project)
    if conv is not None:
        where["conv_id"] = str(conv)
    spend_total = float(_ledger().spent_dec(since=day, where=where or None, deadline_s=deadline_s))
    _require_budget_settlement_within_deadline(spend_started, deadline_s, day, project)
    return spend_total


@_consumer_aggregate_snapshot
def input_cache_split(since=None, project=None, conv=None):
    """Measured input-token cache axes across live ledger rows: read, write, and total processed input.

    The ledger's canonical Claude Code shape is fresh `in_tok` plus the two independent cache columns, so the
    denominator is their sum. Include reconstructed subscription rows because their transcript usage is the primary
    measured cache evidence; exclude reconciliation mirrors so the same usage is not counted twice. The two cache
    axes stay separate and are never inferred."""
    from .ledger import LLM_USD_COLS
    where = {}
    if project is not None:
        where["project_primary"] = str(project).strip().lower()
    if conv is not None:
        where["conv_id"] = str(conv)
    rows = _ledger().sum_by("day", cols=LLM_USD_COLS, filt="COALESCE(reconciled,0)=0", since=since,
                            where=where or None, int_cols=["in_tok", "cache_read_tok", "cache_write_tok"])
    read = sum(int(v.get("cache_read_tok") or 0) for v in rows.values())
    write = sum(int(v.get("cache_write_tok") or 0) for v in rows.values())
    total = sum(int(v.get("in_tok") or 0) + int(v.get("cache_read_tok") or 0)
                + int(v.get("cache_write_tok") or 0) for v in rows.values())
    return {"cache_read_tok": read, "cache_write_tok": write, "input_tok": total,
            "cache_read_share": (read / total if total else 0.0),
            "cache_write_share": (write / total if total else 0.0)}


@bind_ledger_query_deadline
@budget_projection_snapshot
def spent_by_job(job, since=None, *, deadline_s=config.LEDGER_QUERY_DEADLINE_S):
    """Gate-recorded billed LLM $ for ONE job/run tag — the rows whose `chain` == `spendguard.context(chain=<job>)`.
    This is the concurrency-SAFE "my spend": the chain is per-run, so a job's total never sweeps in a CONCURRENT run
    that happens to share the intent (the collision that made `spent_since` filtered by intent over-count). Read from
    spend_events via the same `_COUNTABLE` filter spent_since uses (batch+realtime; excludes meta / reconciliation /
    voided / reconstructed). $0, read-only; 0.0 for an empty job. `since` optionally bounds it to an accounting day on."""
    if not job:
        return 0.0
    job_spend_started = time.monotonic()
    job_spend_total = float(_ledger().spent_dec(since=since, where={"chain": str(job)}, deadline_s=deadline_s))
    _require_budget_settlement_within_deadline(job_spend_started, deadline_s, since)
    return job_spend_total


@bind_ledger_query_deadline
@budget_projection_snapshot
def spent_all_time(*, deadline_s=config.LEDGER_QUERY_DEADLINE_S):
    """Running total of ALL gate-recorded countable LLM billed-$ to date — the flow-receipt BASELINE, read at a
    flow's enter and its exit so the DELTA is what that flow spent (calls.context / receipt.emit_flow). Memoized
    on a ledger-freshness token (SpendLedger.running_llm_total_dec), so opening many flow contexts no longer
    re-sums the whole 356k-row ledger each time. Same number as the old spent_since('1970-01-01') idiom — which
    forced a full SCAN of spend_events on every context enter — without the scan."""
    all_spend_started = time.monotonic()
    all_spend_total = float(_ledger().running_llm_total_dec(deadline_s=deadline_s))
    _require_budget_settlement_within_deadline(all_spend_started, deadline_s)
    return all_spend_total


@_consumer_aggregate_snapshot
def suspect_batches(since):
    """Batch spend since `since`, with the token counts, so an operator can SEE the arithmetic behind a number
    they doubt. `row` is the spend_events id — the exact handle for `spendguard quarantine --row`. Batch rows
    come from in_category('batch') (the batch money column populated — the schema's own definition), and the
    tokens live on the row (in_tok/out_tok) so there's no `calls` join; `actor` carries the caller."""
    rows = _ledger().in_category("batch", since=since)
    out = [{"row": r["id"], "ts": r["ts_utc"], "day": r["day"], "provider": r["provider"] or "?",
            "model": r["model"] or "?", "cost": float(r["batch_usd"] or 0), "project": r["project_primary"] or "",
            "conv_id": r["conv_id"] or "", "in_tok": r["in_tok"], "out_tok": r["out_tok"], "caller": r["actor"] or ""}
           for r in rows]
    return sorted(out, key=lambda x: -x["cost"])


@serialize_ledger_function
def quarantine_charge(ts=None, reason="", row=None):
    """Void ONE spend_events row as an impossible estimate — status='void', so it drops out of every total
    while staying fully auditable (the amount is untouched; the change is logged through the hash chain by
    update()).

    Target by `row` (the spend_events id — exact) or by `ts` (ts_utc). Up to six rows can share one second, so
    a ts matching more than one RAISES rather than voiding all of them: silently voiding five innocent rows to
    quarantine one bad one would be a worse version of the bug this repairs. Returns the number voided."""
    from .ledger import to_dec, USD_COLS
    led = _ledger()
    with _lock:
        if row is not None:
            hit = led.get(row)
            ids = [row] if hit and hit.get("status") != "void" else []
        else:
            hits = [r for r in led.query(where={"ts_utc": ts}) if r.get("status") != "void"]
            if len(hits) > 1:
                raise ValueError(
                    "%d spend_events rows share the timestamp %s (%s) — refusing to void all of them. Re-run "
                    "with --row <id> for the one you mean; `spendguard quarantine --list` shows the ids."
                    % (len(hits), ts, ", ".join("id %s: %s $%s" % (
                        r["id"], r["model"], sum((to_dec(r.get(c)) for c in USD_COLS), to_dec(0))) for r in hits)))
            ids = [hits[0]["id"]] if hits else []
        n = 0
        for eid in ids:
            # update() sets status AND writes the audit row through the hash chain, under the ledger's own
            # connection — the mutation and its tamper record are one operation. It refuses a locked period.
            led.update(eid, {"status": "void"}, actor="quarantine_charge",
                       reason=reason or "impossible estimate", pass_="quarantine")
            n += 1
    return n


@serialize_ledger_function
def unquarantine_charge(row=None, ts=None, reason=""):
    """Reverse quarantine_charge for ONE spend_events row: status 'void'→'posted' so it counts again, and clear
    the '(impossible-estimate)' conv_id label it was tagged with. The change is logged through the hash chain by
    update() (so the void→recovered history is auditable), and a locked period is refused.

    Use ONLY for a row that was wrongly voided. The sound test is `basis == billed` with a real cost: a cost that
    came from the provider's OWN usage means the provider ACCEPTED the request, so it cannot have been an
    impossible estimate — the batched-embedding bug quarantined exactly these (input > window computed as sum/1,
    when the window bounds each item). A cost_basis='estimate' void is a pre-submission projection the provider
    never accepted; those stay quarantined. Target by `row` (exact id) or `ts` (a ts matching >1 RAISES, as in
    quarantine_charge). Returns the number recovered."""
    led = _ledger()
    with _lock:
        if row is not None:
            hit = led.get(row)
            ids = [row] if hit and hit.get("status") == "void" else []
        else:
            hits = [r for r in led.query(where={"ts_utc": ts}) if r.get("status") == "void"]
            if len(hits) > 1:
                raise ValueError(
                    "%d spend_events rows share the timestamp %s — refusing to recover all of them. Re-run with "
                    "--row <id>; `spendguard quarantine --list` shows the ids." % (len(hits), ts))
            ids = [hits[0]["id"]] if hits else []
        n = 0
        for eid in ids:
            cur = led.get(eid) or {}
            changes = {"status": "posted"}
            if cur.get("conv_id") == QUARANTINE_CONV:      # drop the now-false label; keep it if it was something else
                changes["conv_id"] = ""
            led.update(eid, changes, actor="unquarantine_charge",
                       reason=reason or "recovered: provider-billed, wrongly quarantined", pass_="unquarantine")
            n += 1
    return n


@_consumer_aggregate_snapshot
def unpriced_since(day):
    """[{provider, model, calls}] of calls recorded with NO price since `day` — the cost_basis='unpriced' rows.
    The tokens are real; only the dollars are unknown. Surfacing this is the difference between "we spent $0"
    and "we cannot tell you what we spent" — and the second one is actionable (`spendguard price <model> …`)."""
    res = _ledger().sum_by(["provider", "model"], since=day, where={"cost_basis": "unpriced"})
    out = [{"provider": p or "?", "model": m or "?", "calls": v["n"]} for (p, m), v in res.items()]
    return sorted(out, key=lambda x: -x["calls"])


def record_unpriced(provider, model, kind, in_tok=0, out_tok=0, project=None):
    """Record that a call HAPPENED but could not be priced. cost=0 because we refuse to invent a number, but
    the row is MARKED so no total treats it as 'free' and the receipt can name the model to price."""
    now = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")
    proj = (project if project is not None else _project()) or ""
    # a $0 forensic marker (cost_basis='unpriced'), carrying the real tokens the price is unknown for.
    _record_spend_event(provider, model, kind, 0, basis=BASIS_UNPRICED, project=proj,
                        occurred_at=now, in_tok=in_tok, out_tok=out_tok, source="gate", dedup_suffix=":unpriced")


def reprice_unpriced(model=None, apply=False, actor="reprice"):
    """Retroactively PRICE 'unpriced' spend_events rows that are NOW priceable (a rate resolves for their exact
    (provider, model)). The tokens were always real; only the $ was unknown at gate time — the model had no price
    yet, or its provider was ambiguous. This gives each its catalog-rate cost through the ledger's OWN correction
    path: update() revises an OPEN row in place (cost_basis 'unpriced'→'estimate', sets the money column + rates,
    so it drops out of the 'cannot price' view AND folds into the total); a row in a LOCKED period gets an adjust()
    DELTA instead (the $ folds in; the immutable original keeps its marker, so a closed period is never edited).
    A row whose price STILL can't resolve is LEFT unpriced — honest, never a guessed number.

    DRY-RUN by default: returns a plan [{id, provider, model, in_tok, out_tok, cost, method}] (method ∈
    update|adjust|skip). `apply=True` commits through update()/adjust() — both audited + revision-bumped, never raw
    SQL. Filter to one `model` (e.g. 'glm-5.2') or sweep every unpriced model."""
    from . import pricing
    from .ledger import _KIND_TO_USD, dec
    from .ledger_writer import current_ledger_writer_connection, run_ledger_write
    if current_ledger_writer_connection() is None:
        return run_ledger_write(lambda: reprice_unpriced(model, apply, actor))
    led = _ledger()
    sql = "SELECT id FROM spend_events WHERE cost_basis=?"
    args = [BASIS_UNPRICED]
    if model:
        sql += " AND model=?"
        args.append(model)
    with _lock:
        ids = [r[0] for r in led._conn.execute(sql, args).fetchall()]
    plan = []
    for eid in ids:
        row = led.get(eid) or {}
        prov, mdl = row.get("provider") or "?", row.get("model") or "?"
        kind = (row.get("cost_type") or "realtime").lower()
        col = _KIND_TO_USD.get(kind)
        in_t = None if row.get("in_tok") is None else int(row["in_tok"])
        out_t = None if row.get("out_tok") is None else int(row["out_tok"])
        ent = {"id": eid, "provider": prov, "model": mdl, "in_tok": in_t, "out_tok": out_t, "cost": None}
        if in_t is None or out_t is None:
            ent.update(method="skip", reason="unknown token usage; value unknown")
            plan.append(ent)
            continue
        if not col:
            ent.update(method="skip", reason=f"no money column for kind {kind!r}")
            plan.append(ent)
            continue
        try:                                              # price ONLY what resolves for this exact (provider, model)
            cost = float(pricing.realtime_cost(mdl, in_t, out_t, provider=prov) or 0.0)
        except Exception as e:                            # still ambiguous / genuinely unpriced → leave it, never guess
            ent.update(method="skip", reason=f"still unpriceable: {str(e)[:70]}")
            plan.append(ent)
            continue
        if cost <= 0:
            ent.update(method="skip", reason="resolved to $0")
            plan.append(ent)
            continue
        pr = pricing.price(mdl, provider=prov) or {}
        ent["cost"] = round(cost, 6)
        ent["_changes"] = {col: dec(cost), "cost_basis": BASIS_ESTIMATE,
                           "rate_in": pr.get("in_"), "rate_out": pr.get("out")}
        ent["method"] = "adjust" if led._is_period_locked(row.get("period")) else "update"
        plan.append(ent)
    if apply:
        for p in plan:
            if p.get("method") == "update":
                led.update(p["id"], p["_changes"], actor=actor, reason="reprice: model now priceable")
            elif p.get("method") == "adjust":
                led.adjust(p["id"], p["_changes"], actor=actor, reason="reprice: model now priceable")
    for p in plan:
        p.pop("_changes", None)                           # internal build detail — not part of the returned plan
    return plan


@_consumer_aggregate_snapshot
def quarantined_since(day):
    """[{day, provider, model, cost, project, n}] of QUARANTINED rows (status=void) since `day` — estimates the
    gate proved impossible. They are kept (forensics: what the estimator claimed, and when) and excluded from
    every total, so the honest thing is to SHOW them rather than let them vanish. include_void: these ARE the
    voided rows, which every other reader excludes."""
    res = _ledger().sum_by(["day", "provider", "model", "project_primary"], since=day,
                           where={"status": "void"}, include_void=True)
    out = [{"day": d, "provider": p or "?", "model": m or "?", "cost": float(v["usd"]), "project": pr or "", "n": v["n"]}
           for (d, p, m, pr), v in res.items()]
    return sorted(out, key=lambda x: -x["cost"])


@_consumer_aggregate_snapshot
def by_basis(day):
    """{basis: {'cost': $, 'n': rows}} since `day` — how much of the headline is a projection vs a bill, read
    from cost_basis on spend_events. Excludes meta + reconciliation mirrors + quarantine (void) + unpriced.
    Unlabelled legacy rows come back under '' and are shown as unknown, never folded into 'billed'."""
    led = _ledger()
    res = led.sum_by("cost_basis", since=day, where={"is_meta": 0, "reconciled": 0}, filt=led._NOT_UNPRICED)
    return {(b or ""): {"cost": float(v["usd"]), "n": v["n"]} for b, v in res.items()}


# ── reconciliation: make the LOCAL ledger reflect PROVIDER-billed truth (the gap = ungoverned/pre-ledger spend) ──
@_consumer_aggregate_snapshot
def by_provider_day(kind=None, since=None):
    """{(provider, day): $} of GATE-recorded COUNTABLE spend — the attributed side of reconcile. Read from
    spend_events via the single _COUNTABLE filter (reconciliation mirrors, meta, quarantine, reconstructed all
    excluded — like-for-like vs provider truth). `kind` restricts to that category's money column."""
    led = _ledger()
    cols = [led.category_col(kind)] if kind else None
    res = led.sum_by(["provider", "day"], cols=cols, filt=led._COUNTABLE, since=since)
    return {(p or "?", d): float(v["usd"]) for (p, d), v in res.items() if float(v["usd"]) != 0}


@_consumer_aggregate_snapshot
def reconciled_by_project(since=None):
    """{project: $} of RECONCILED batch-marker rows (recon_marker='(provider-batch)' — the provider-truth gap
    reconcile_into_ledger attributed by evidence). The 'attributed' side of the reconcile loop, complement to
    gate_by_project_day."""
    res = _ledger().sum_by("project_primary", since=since, where={"recon_marker": _RECONCILED})
    return {(p or "unattributed"): float(v["usd"]) for p, v in res.items()}


@_consumer_aggregate_snapshot
def gate_by_project_day(kind=None, since=None):
    """{(project, day): $} of GATE-recorded (attributed) COUNTABLE spend — read from spend_events via _COUNTABLE
    (reconciliation mirrors/meta/quarantine/reconstructed excluded). Used to compute the per-project gap so the
    provider-truth gap is attributed by evidence, not dumped in one 'unattributed' bucket."""
    led = _ledger()
    cols = [led.category_col(kind)] if kind else None
    res = led.sum_by(["project_primary", "day"], cols=cols, filt=led._COUNTABLE, since=since)
    return {(p or "unattributed", d): float(v["usd"]) for (p, d), v in res.items() if float(v["usd"]) != 0}


def record_reconciled(day, provider, cost, project="unattributed", kind="batch", model=None):
    """Insert a reconciliation row for provider-billed (batch) OR gate-logged (realtime) spend — the gap — attributed
    to `project` by evidence ('unattributed' only when there's none). Marked by a marker model so it's excluded from
    gate/cap and rebuilt idempotently. Default marker '(provider-batch)' / kind 'batch'; the realtime backfill passes
    its own marker + kind='realtime'."""
    marker = model or _RECONCILED
    # basis=BILLED: a reconciliation row IS the provider's own number, not a projection of ours. The marker
    # model → reconciled=1 + recon_marker, so it is excluded from the gate/cap totals.
    # invoice_id = a PERIOD-level provider-invoice reference (provider:YYYY-MM). The batch API exposes no raw invoice
    # number; this groups reconciled rows to the provider's monthly invoice (FOCUS InvoiceId), and is stamped ONLY on
    # rows we reconciled to the bill — an unreconciled estimate row keeps invoice_id NULL, so it never claims a bill.
    _record_spend_event(provider, marker, kind, float(cost), basis=BASIS_BILLED, project=project or "unattributed",
                        occurred_at=day + "T00:00:00+00:00", source="reconcile", dedup_suffix=":recon",
                        invoice_id="%s:%s" % (provider, (day or "")[:7]))


@serialize_ledger_function
def clear_reconciled(since=None, model=None, providers=None):
    """Remove prior reconciliation rows so reconcile is idempotent (rebuilds them). Keyed by the marker model
    (default the batch marker; the realtime backfill passes its own)."""
    snapshot_once("clear-reconciled")           # DELETEs money rows — never without a recovery path
    marker = model or _RECONCILED
    # Delete the reconciliation-mirror rows from spend_events (the money-of-record). NOT charges: its writer
    # record_reconciled writes only spend_events (the INSERT INTO charges was removed 6 lines above in this
    # same change), reconciled_by_project reads spend_events (repointed 5b, commit 6969746), and `grep
    # 'FROM charges'` across src is EMPTY. So charges holds no reconciliation rows to leave stale.
    if providers is None:
        _ledger().delete(where={"recon_marker": marker}, since=since, actor="clear_reconciled", reason="rebuild")
    else:
        for provider in providers:
            _ledger().delete(where={"recon_marker": marker, "provider": provider}, since=since,
                             actor="clear_reconciled", reason="rebuild")


# ── estimate→actual true-down (ledger_sync.true_down writes these; the gate's batch rows are PRE-SUBMIT
#    estimates, so once the provider bills the actuals the ledger must come down to the billed truth) ──
# RAW-CHARGES-OK: excludes true-down as well, which the view deliberately keeps
@_consumer_aggregate_snapshot
def gate_batch_cells(since=None):
    """{(project, provider, model, day): $} of GATE-LIVE batch rows — the estimate base the true-down corrects.
    Excludes every reconcile marker model AND prior true-down rows (idempotence: corrections never feed the next
    correction). Full-dimension sibling of gate_by_project_day."""
    led = _ledger()
    # sum the BATCH money column only; exclude reconciliation markers (reconciled=0) and prior true-down rows
    # (the true-down conv sentinel) so a correction never feeds the next correction. Quarantine (void) is auto-out.
    filt = "COALESCE(conv_id,'') != '%s'" % _TRUE_DOWN_CONV
    res = led.sum_by(["project_primary", "provider", "model", "day"], cols=["batch_usd"],
                     where={"reconciled": 0}, filt=filt, since=since)
    return {(pr or "unattributed", p or "?", m or "?", d): float(v["usd"])
            for (pr, p, m, d), v in res.items() if float(v["usd"]) != 0}


def record_true_down(day, provider, model, delta, project, *, maintenance_ledger=None, posting_metadata=None):
    """Insert ONE negative batch correction row: estimate − billed for this cell's share. Carries the REAL model
    (so by_dims nets it against the estimate rows before the SaaS push) and the true-down conv_id sentinel (so it
    is identifiable/clearable without a marker model). The original estimate rows are NEVER mutated — the ledger
    keeps both the estimate and the correction (forensic: what we thought + what it actually billed)."""
    # A NEGATIVE batch row carrying the REAL model + the true-down conv sentinel (nets the estimate down).
    # gate_batch_cells (the estimate base) and clear_true_down were BOTH repointed to spend_events in 5b (commit
    # 6969746), and a `grep 'FROM charges'` sweep across src is EMPTY — no reader reads charges, so writing only
    # spend_events here is complete, not partial.
    _record_spend_event(provider, model, "batch", -abs(float(delta)), conv_id=_TRUE_DOWN_CONV,
                        project=project or "unattributed", occurred_at=day + "T00:00:00+00:00",
                        source="true-down", dedup_suffix=":td",
                        invoice_id="%s:%s" % (provider, (day or "")[:7]),
                        maintenance_ledger=maintenance_ledger,
                        posting_metadata=posting_metadata)   # reconciled to this provider-period invoice


def clear_true_down(since=None):
    """Clear every provider unconditionally: an estimate is more honest than stale billed truth.

    A failed billed fetch must fall back to estimates, never retain an old correction.
    Ledger-read failures use the separate readable-provider rebuild operation instead.
    """
    return clear_readable_true_down(since)


def clear_readable_true_down(since=None, providers=None, *, maintenance_ledger=None):
    """Remove prior true-down rows in the window so the correction is rebuilt idempotently each reconcile
    (billed actuals only grow as in-flight batches land, so each rebuild converges on the final billed $).

    CLEARS EVERY PROVIDER ON PURPOSE, including one whose billed fetch just failed. A review finding said
    this was a bug — clearing corrections for a provider we could not read — and test_true_down.py, which
    predates it, says otherwise and explains why: a failed fetch means that provider is NEVER trued down,
    so its rows fall back to the gate ESTIMATE, which is labelled `basis=estimate` and reads as one.
    Keeping the previous correction would leave a figure LABELLED as billed truth while being derived from
    billed data this run could not confirm — a stale number wearing a stronger label. The estimate is the
    more honest of the two, so the clear stays unconditional and ledger_sync warns about the gap instead.
    Second finding today that both validators confirmed and the code was right about.

    An explicit providers collection scopes the rebuild to readable provider units; None preserves the
    unconditional default above. An empty collection changes nothing, including taking no snapshot."""
    from .ledger_writer import current_ledger_writer_connection, run_ledger_write
    if maintenance_ledger is None and current_ledger_writer_connection() is None:
        return run_ledger_write(lambda: clear_readable_true_down(since, providers))
    true_down_provider_scope = None if providers is None else tuple(dict.fromkeys(providers))
    if true_down_provider_scope == ():
        return
    snapshot_once("clear-true-down")    # DELETEs money rows — never without a recovery path
    # Delete the true-down correction rows from spend_events (the money-of-record). NOT charges: the ONLY writer
    # of true-down rows, record_true_down, now writes only spend_events (its INSERT INTO charges was removed
    # above); and the only consumers of these rows — gate_batch_cells (the estimate base) and by_dims (the SaaS
    # push) — read spend_events (repointed 5b). So charges holds no true-down rows for this to leave stale.
    from .ledger import LockedError
    from .ledger_read import DEFAULT_LEDGER_READ_ROW_CAP
    import logging as true_down_logging
    import traceback as true_down_traceback
    true_down_ledger = maintenance_ledger if maintenance_ledger is not None else _ledger()
    true_down_scopes = ([{"conv_id": _TRUE_DOWN_CONV}] if true_down_provider_scope is None else
                        [{"conv_id": _TRUE_DOWN_CONV, "provider": true_down_provider}
                         for true_down_provider in true_down_provider_scope])
    try:
        # Preserve delete's full lock preflight before the first destructive batch, without retaining all matches.
        for true_down_scope in true_down_scopes:
            true_down_filter, true_down_args = true_down_ledger._where(since, None, true_down_scope)
            true_down_cursor = true_down_ledger._conn.execute(
                f"SELECT id, period FROM spend_events WHERE 1=1{true_down_filter} "
                "AND (COALESCE(locked,0) != 0 OR "
                "(COALESCE(period,'') != '' AND period <= (SELECT MAX(period) FROM ledger_locks)))",
                true_down_args)
            try:
                true_down_locked_row = true_down_cursor.fetchone()
                if true_down_locked_row:
                    true_down_id, true_down_period = true_down_locked_row
                    raise LockedError(f"refusing to delete locked event {true_down_id} "
                                      f"(period {true_down_period}) — use reverse/adjust")
            finally:
                true_down_cursor.close()
        for true_down_scope in true_down_scopes:
            true_down_filter, true_down_args = true_down_ledger._where(since, None, true_down_scope)
            while True:
                true_down_rowids = true_down_ledger._conn.execute(
                    f"SELECT rowid FROM spend_events WHERE 1=1{true_down_filter} LIMIT ?",
                    (*true_down_args, DEFAULT_LEDGER_READ_ROW_CAP)).fetchall()
                if not true_down_rowids:
                    break
                # rowid is a SQLite integer, not source content; parse before forming the internal SQL predicate.
                true_down_rowid_sql = ",".join(str(int(true_down_rowid[0])) for true_down_rowid in true_down_rowids)
                del true_down_rowids  # release this batch before delete() allocates its bounded audit batch.
                true_down_ledger.delete(where=true_down_scope, filt=f"rowid IN ({true_down_rowid_sql})", since=since,
                                        actor="clear_true_down", reason="rebuild")
    except Exception as true_down_clear_error:
        true_down_logging.getLogger(__name__).exception("refusing clear_true_down after ledger clear failure")
        if maintenance_ledger is not None or isinstance(true_down_clear_error, LockedError):
            raise
        raise RuntimeError(
            f"refusing clear_true_down after ledger clear failure: {true_down_clear_error!r}\n"
            f"{true_down_traceback.format_exc()}") from true_down_clear_error


# ── spendguard's own advisor LLM use (segregated: own cap, own line, excluded from workload) ──
def record_meta(provider, model, cost):
    # spendguard's OWN spend → the llm-spendguard project, kept distinct by kind='meta' (NOT a separate project tag).
    record_charge(provider, model, "meta", cost, project="llm-spendguard")


def record_external_cost(provider, service, cost, conv_id=None, intent=None, actor=None, project=None):
    """Record ONE non-token EXTERNAL cost — an MCP/tool call or a paid external (non-LLM) API — as a first-class
    ledger row on the `external` axis: its own real-$ column (BILLED_USD_COLS), kept apart from LLM token spend, GPU/
    remote, and the flat subscription fee. `service` is the tool/endpoint identity (recorded in the model slot so the
    receipt and FOCUS export name WHAT was paid for); conv_id/intent/actor attribute it to the trace exactly like an
    LLM charge. This is the RAIL an instrumented tool/MCP wrapper calls after a paid call — real $ out the door, so it
    joins the receipt's REAL-$ total. Auto-interception and a dedicated external spend cap are a follow-on."""
    _record_spend_event(provider or "external", service or "?", "external", float(cost),
                        conv_id=conv_id or "", intent=intent or "", actor=actor or "",
                        project=project or "", source="external")


@budget_projection_snapshot
def external_spent_since(day):
    """Real EXTERNAL (MCP/tool + external API) $ recorded since `day`, from the ledger's external axis — its OWN
    number, never mixed into the LLM cap (spent_since reads only batch+realtime)."""
    projection_ledger = _ledger()
    return round(float(projection_ledger._bounded_category_total(
        (projection_ledger.category_col("external"),),
        projection_ledger._LIVE_FILTER + " AND " + projection_ledger._NOT_UNPRICED,
        since=day, where={"is_meta": 0})), 6)


def external_spent_today():
    return external_spent_since(_utc().strftime("%Y-%m-%d"))


@finite_budget_projection
def external_exceeded(pending=0.0):
    """(‑, cap, projected) if today's external spend + `pending` would breach the external cap, else None. ONE read
    of the spent total so the value compared and the value reported can't diverge (same discipline as meta_exceeded)."""
    cap = config.external_cap()
    if cap is None:
        return None                                     # no external cap set → gating is a no-op (opt-in)
    spent = external_spent_today() + float(pending or 0)
    require_finite_budget_value((cap, spent))
    return ("external", cap, spent) if spent > cap else None


@budget_projection_snapshot
def meta_spent_since(day):
    """spendguard's OWN (meta) spend since `day` — the is_meta rows, on their own cap line. Read from
    spend_events via meta_dec (excludes voided/reversed; a meta row is never quarantine/unpriced)."""
    return float(_ledger().meta_dec(since=day))


def meta_spent_today():
    return meta_spent_since(_utc().strftime("%Y-%m-%d"))


@finite_budget_projection
def meta_exceeded(pending=0.0):
    cap = config.meta_cap()
    # ONE READ. Called twice, the value compared against the cap and the value REPORTED to the user were
    # two different measurements with a concurrent write possible between them — so the message could name
    # a total that does not breach the cap it says was breached, which reads as a bug in the cap itself.
    spent = meta_spent_today() + pending
    require_finite_budget_value((cap, spent))
    if cap is not None and spent > cap:
        return ("meta", cap, spent)
    return None


# RAW-CHARGES-OK: carries its own kind/meta switch per caller; markers already excluded
@_consumer_aggregate_snapshot
def by_day(kind=None, exclude_meta=False, since=None, exclude_reconciled=False):
    """{day: total$} from the local ledger, optionally filtered by kind / excluding meta / excluding reconciled
    (provider-truth) rows / since a date. exclude_reconciled is essential for the LEAK check AND the trust check:
    reconciled rows MIRROR truth from another source (provider batch / gate log / reconstruction), so counting them
    as 'local gate-recorded' double-counts against that very source (coverage >100%, trust ratio inflated). It
    excludes ALL marker models; true-down correction rows are NOT markers (they correct the gate's own estimates)
    and stay included."""
    # Quarantined rows (status=void) + unpriced are excluded UNCONDITIONALLY. They are not spend anybody made;
    # counting them as "accounted" is what let an invented $54.51 sit inside a leak check that then reported no
    # leak. void is auto-out of sum_by; unpriced via _NOT_UNPRICED.
    led = _ledger()
    where = {}
    if exclude_meta:
        where["is_meta"] = 0
    if exclude_reconciled:
        where["reconciled"] = 0
    if kind == "meta":
        where["is_meta"] = 1
        cols = None
    elif kind:
        cols = [led.category_col(kind)]
        where.setdefault("is_meta", 0)   # a category kind is workload-only: meta banks in realtime_usd but is its OWN kind
    else:
        cols = None
    res = led.sum_by("day", cols=cols, where=where or None, filt=led._NOT_UNPRICED, since=since)
    return {d: float(v["usd"]) for d, v in res.items() if float(v["usd"]) != 0}


# RAW-CHARGES-OK: the SaaS PUSH payload deliberately carries backfill AND meta rows — the org
# dashboard needs them. countable_charges strips both, so routing this through it silently
# changed what we ship upstream; tests/test_ledger_marker_matrix.py caught it.
@_consumer_aggregate_snapshot
def by_dims(since=None):
    """Per-day rows grouped by (day, provider, model, kind) for the SaaS roll-up push — the structured shape
    the server's /v1/ledger expects (vs by_day's flat {day: $}). Returns dicts with cost in $ and a call count.

    ACTUAL-$ AXIS ONLY. This feeds the actual-$ roll-up (/v1/ledger), so it sums ONLY the billed money columns
    (BILLED_USD_COLS — never est_chat_usd) and drops est-VALUE (est_chat) rows outright. est-value is plan-covered,
    NOT real money, and must NEVER enter an actual-$ total; it reaches the server on its OWN axis (the chat loop +
    lane_value), never here. Two enforcements — the billed-only sum makes est-value dollars $0 in `cost`, and the
    est_chat skip drops the now-empty row — so no est-value can leak even if a row is mis-categorized."""
    # This is the SaaS PUSH payload. A quarantined row here would put an invented number on the org dashboard,
    # where nobody has the local context to question it — so void is auto-excluded, and unpriced ($0) too. It
    # DELIBERATELY keeps reconciliation/backfill + meta rows: the org dashboard needs them. `kind` is
    # reconstructed from the row's category — is_meta → 'meta', else the category its money column names.
    from .ledger import BILLED_USD_COLS
    led = _ledger()
    # est-VALUE is excluded in the SQL WHERE (billed=0 rows never enter the sum), exactly like void/unpriced —
    # NOT filtered out row-by-row in Python, so there is no untraced drop. COALESCE(billed,1) keeps legacy rows
    # (default 1) and drops only the explicit billed=0 est_chat rows. cols=BILLED_USD_COLS is belt-and-suspenders:
    # even a mis-labeled row contributes $0 of est_chat_usd to the actual-$ total.
    res = led.sum_by(["day", "provider", "model", "cost_type", "is_meta", "project_primary"],
                     cols=BILLED_USD_COLS, since=since, filt=led._NOT_UNPRICED + " AND COALESCE(billed, 1) = 1",
                     int_cols=["in_tok", "out_tok", "cache_read_tok"])   # tokens for the SaaS unit-econ view
    out = []
    for (day, prov, model, ctype, ismeta, proj), v in res.items():
        kind = "meta" if ismeta else (ctype or "workload")
        out.append(dict(day=day, provider=prov or "?", model=model or "?", kind=kind,
                        project=proj or "", cost=float(v["usd"]), calls=v["n"],
                        in_tok=v.get("in_tok", 0), out_tok=v.get("out_tok", 0),
                        cache_read_tok=v.get("cache_read_tok", 0)))
    return out


# RAW-CHARGES-OK: excludes true-down as well, which the view deliberately keeps
@_consumer_aggregate_snapshot
def by_key(since=None):
    """{(provider, key_fp): {'cost': $, 'calls': n}} of gate-recorded workload — per-KEY spend (which
    workspace/project key did this money flow through?). Excludes meta and every reconcile marker row
    (mirror/backfill rows carry no key). key_fp '' groups as '(none)' — rows recorded before key stamping
    existed, or where no key env was resolvable. LOCAL-ONLY view; fingerprints never leave the machine."""
    led = _ledger()
    # exclude meta + reconciliation markers (reconciled=0) + true-down (mirror/correction rows carry no key) +
    # unpriced; quarantine (void) is auto-out. key_fp '' → '(none)'.
    filt = "%s AND COALESCE(conv_id,'') != '%s'" % (led._NOT_UNPRICED, _TRUE_DOWN_CONV)
    res = led.sum_by(["provider", "key_fp"], where={"is_meta": 0, "reconciled": 0}, filt=filt, since=since)
    return {(p or "?", fp or "(none)"): {"cost": float(v["usd"]), "calls": v["n"]} for (p, fp), v in res.items()}


@_consumer_aggregate_snapshot
def by_intent(since=None):
    """{intent: {'cost': $, 'calls': n}} of gate-recorded COUNTABLE LLM workload since `since` — billed-$ grouped by
    the forensic `intent` label, so "what did each job-type cost" is a command (`receipt --by-intent`), not the
    hand-written spend.db query a caller had to run to verify it. Uses the SAME money columns (LLM_USD_COLS) and the
    SAME countable filter (`_COUNTABLE`) as spent_since — the receipt's real-$ API line — so the Σ over all intents
    RECONCILES with that headline for the same window, exactly. Blank/legacy intent groups under '(unlabelled)' and is
    never dropped (an unattributed dollar stays visible, not silently folded into a named job)."""
    from .ledger import LLM_USD_COLS
    led = _ledger()
    res = led.sum_by(["intent"], cols=LLM_USD_COLS, filt=led._COUNTABLE, since=since)
    return {(i or "(unlabelled)"): {"cost": float(v["usd"]), "calls": v["n"]} for i, v in res.items()}


@_consumer_aggregate_snapshot
def billed_by_model(intent=None, as_of=None):
    """{'provider:model': billed $} of COUNTABLE LLM spend from the LEDGER (money of record), optionally scoped to one
    `intent` and/or AS-OF a date (`day <= as_of`, matching advise's backtest window). This is the COST BASIS advise /
    best-value should rank on — the LEDGER, not the calls corpus, whose per-call cost figures diverge wildly for
    lane-routed calls (a lane bills ~$0 but the corpus booked a metered-EQUIVALENT per call: measured ~$1,019 corpus vs
    ~$18 billed for one model/month). A lane model reads ~$0 billed here; the caller (advise.ranked) floors it to its
    amortized plan-draw (out_tok x lane_eff) so a $0-lane arm is priced at its TRUE marginal cost, never a flat $0 and
    never the corpus fiction. Keyed 'provider:model' to match advise.evidence's row ids. Same LLM_USD_COLS + _COUNTABLE
    filter as spent_since, so a metered arm's cost here reconciles with the real-$ ledger for that (intent, model)."""
    from .ledger import LLM_USD_COLS
    led = _ledger()
    where = {"intent": intent} if intent is not None else None
    res = led.sum_by(["provider", "model"], cols=LLM_USD_COLS, filt=led._COUNTABLE, where=where, until=as_of)
    return {f"{(p or '?')}:{(m or '?')}": float(v["usd"]) for (p, m), v in res.items()}


@_consumer_aggregate_snapshot
def ledger_start(kind=None):
    """Earliest day in the ledger — spend before this wasn't recorded locally (pre-ledger). With `kind`, the
    earliest day for THAT category (its money column populated): axes start recording at different times
    (realtime began long before batch), so a per-axis cutoff keeps the leak check from mislabeling one axis's
    pre-history as a leak."""
    led = _ledger()
    if kind:
        days = [r["day"] for r in led.in_category(kind) if r["day"]]
        return min(days) if days else None
    return led.min_day()


def _utc():
    return datetime.datetime.now(datetime.timezone.utc)


def spent_today():  return spent_since(_utc().strftime("%Y-%m-%d"))
def spent_month():  return spent_since(_utc().strftime("%Y-%m-01"))


@finite_budget_projection
def exceeded(pending=0.0, kind="llm", *, deadline_s=config.LEDGER_QUERY_DEADLINE_S):
    """(scope, cap, projected) if a cap would be exceeded by `pending` more $ on a call of resource class
    `kind` (llm|compute), else None. Checks the class SUB-CAP then the TOTAL ceiling, daily then monthly.
    The gate governs LLM calls, so it passes kind='llm'; remote-compute caps are checked in resources.py
    (vast.ai launches don't hit the gate). meta is separate (meta_exceeded). NOTE: this local ledger holds LLM
    spend, so the total ceiling here is evaluated against LLM spend — the true LLM+compute total is composed on
    the dashboard/report; the compute portion is enforced/alerted via resources.compute_exceeded()."""
    if deadline_s is None:
        sd, sm = spent_today(), spent_month()
    else:
        expires = time.monotonic() + deadline_s
        sd = spent_since(_utc().strftime("%Y-%m-%d"), deadline_s=expires - time.monotonic())
        sm = spent_since(_utc().strftime("%Y-%m-01"), deadline_s=expires - time.monotonic())
    checks = []
    if kind in ("llm", "compute"):
        checks.append((f"{kind}-daily", config.class_cap(kind, "daily"), sd))
        checks.append((f"{kind}-monthly", config.class_cap(kind, "monthly"), sm))
    # Through the NAMED accessors, so `config.daily_cap`/`config.monthly_cap` are the one place each ceiling
    # is defined rather than two spellings of the same lookup. The unwired-capability scan flagged
    # monthly_cap as an unenforced control — it was wrong about the impact (the ceiling IS checked, right
    # here, via class_cap) but right that the accessor had no caller, which is how a reader grepping for
    # `monthly_cap` concludes nothing enforces it and writes a second one.
    checks.append(("total-daily", config.daily_cap(), sd))
    checks.append(("total-monthly", config.monthly_cap(), sm))
    for scope, capv, sp in checks:
        require_finite_budget_value((capv, sp, sp + pending))
        if capv is not None and sp + pending > capv:
            return (scope, capv, sp + pending)
    return None

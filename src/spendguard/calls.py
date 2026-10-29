"""Rich per-call context log (opt-in) — turns spend records into a cost+QUALITY corpus.

OFF by default (it can store prompts/outputs — privacy). Enable with config calls.enabled=true
(or SPENDGUARD_CALLS=1). Per call it records: chain, intent, caller, model, cost, tokens, latency,
prompt hash (+ optional snippet), output snippet, finish reason, and a DEFERRED quality label:
  - feedback(call_id, ok)        -> explicit / judge verdict (authoritative)
  - reuse alone                 -> unjudged (NULL); only feedback or a judge can assess quality
That powers cost-per-GOOD-result per intent (`spendguard calls`) and, later, an `optimize` loop.

Shares the SQLite db with budget (config.db_path()), table `calls`. RLock — reentrant (record/query
hold it and call _calls_db() which re-acquires).
"""
import os, datetime, threading, hashlib, inspect, contextlib, uuid
import logging
from decimal import Decimal, localcontext
from typing import Optional

from . import config
from .ledger_fork_locks import new_fork_reset_ledger_state
from .schema_indexes import ensure_ordered_index_coverage
from .money_scale import NANO_USD_PER_USD, real_usd_to_nano_usd, nano_usd_to_usd, assert_nano_total_matches

CALL_OBSERVATION_NANO_COLUMNS = {column: f"{column}_nano" for column in ("cost", "input_cost_floor")}


def _call_observation_nano_amounts(cost, input_cost_floor):
    # Mirrors are derived: public writers have fixed signatures rejecting nano
    # keyword arguments; callers supply only the preserved REAL amounts.
    return tuple(None if amount is None else real_usd_to_nano_usd(float(amount))
                 for amount in (cost, input_cost_floor))


def _backfill_call_observation_nano_columns(connection, missing_columns):
    conversion_gaps = []
    for raw_column, nano_column in CALL_OBSERVATION_NANO_COLUMNS.items():
        if nano_column not in missing_columns:
            continue
        for storage_row_id, row_id, raw_amount in connection.execute(f"SELECT rowid,id,{raw_column} FROM calls"):
            try:
                nano_amount = None if raw_amount is None else real_usd_to_nano_usd(raw_amount)
            except (ValueError, TypeError, OverflowError) as conversion_error:
                conversion_gaps.append(((storage_row_id, row_id), raw_column, str(conversion_error)))
                continue
            connection.execute(f"UPDATE calls SET {nano_column}=? WHERE rowid=?", (nano_amount, storage_row_id))
    if conversion_gaps:
        raise ValueError(f"call_observation_nano_invalid_amount_gap count={len({gap[0] for gap in conversion_gaps})} "
                         f"amount_count={len(conversion_gaps)} rows={conversion_gaps!r}")
    for raw_column, nano_column in CALL_OBSERVATION_NANO_COLUMNS.items():
        nano_total = sum(row[0] for row in connection.execute(
            f"SELECT {nano_column} FROM calls WHERE {nano_column} IS NOT NULL"))
        converted_total = sum(real_usd_to_nano_usd(row[0]) for row in connection.execute(
            f"SELECT {raw_column} FROM calls WHERE {raw_column} IS NOT NULL"))
        # Integer accumulation followed by Decimal scaling is an exact Decimal sum,
        # independent of ambient Decimal precision and SQLite SUM integer overflow.
        assert_nano_total_matches(nano_total, nano_usd_to_usd(converted_total),
                                  tolerance_usd='0', label=f"calls.{raw_column}")
        float_total = connection.execute(f"SELECT SUM({raw_column}) FROM calls").fetchone()[0]
        # Nano is now more precise than the historical binary-float SUM; this delta
        # is informational, never the equality gate above.
        exact_usd = nano_usd_to_usd(nano_total)
        historical_usd = Decimal(str(float_total or 0))
        with localcontext() as delta_context:
            carry_digits = 2
            delta_context.prec = (max(exact_usd.adjusted(), historical_usd.adjusted())
                                  - min(exact_usd.as_tuple().exponent, historical_usd.as_tuple().exponent)
                                  + carry_digits)
            delta = exact_usd - historical_usd
        logging.getLogger(__name__).info("calls_nano_float_sum_delta column=%s delta_usd=%s", raw_column, delta)

_lock = new_fork_reset_ledger_state(globals(), "_lock", threading.RLock)
_local = threading.local()
_PKG = os.path.dirname(os.path.abspath(__file__))
_ORIGIN_SESSION = uuid.uuid4().hex[:12]
_CALL_CLASSES = frozenset(("workload", "gate_internal", "probe"))


# ── opt-in flags ──
# Compatibility alias; internal calls use the shared home to survive partial module overlays.
_truthy = config.is_explicit_true


def call_recording_consent_enabled():
    # PRIVACY-CRITICAL: this can store prompts/outputs, so it FAILS CLOSED. Recording is ON only for an EXPLICIT
    # affirmative env value; ANY other value — "False", "off", "disable", a typo, blank — leaves it OFF. The old
    # check was a false-word BLOCKLIST (`not in ("0","false","")`), so `SPENDGUARD_CALLS=False` was not matched and
    # silently ENABLED recording, the opposite of intent. An affirmative allowlist is the only safe direction for a
    # privacy switch: the unknown case must be "don't record", never "record". (Mirrors _truthy, case-insensitively.)
    v = os.getenv("SPENDGUARD_CALLS")
    if v is not None and v.strip() != "":
        return v.strip().lower() in ("1", "true", "yes", "on")
    return config.is_explicit_true(config._cfg_get("calls", "enabled", False))


def _store_prompts(): return config.is_explicit_true(config._cfg_get("calls", "store_prompts", False))
def _snip():          return int(config._cfg_get("calls", "snippet_len", 200) or 200)


# ── intent / chain context (thread-local; safe under ThreadPool) ──
def current():
    return getattr(_local, "ctx", {})


def set_context(intent: Optional[str] = None, chain: Optional[str] = None, who: Optional[str] = None,
                root_call: Optional[str] = None, attempt: Optional[int] = None,
                fell_from: Optional[str] = None, fell_reason: Optional[str] = None,
                batch_expected_out: Optional[int] = None,
                call_class: Optional[str] = None, origin_session: Optional[str] = None,
                defer_batch_booking: Optional[bool] = None, requested_model: Optional[str] = None,
                redirect_reason: Optional[str] = None, resolved_lane: Optional[str] = None,
                measurement: Optional[bool] = None, gen_params: Optional[dict] = None,
                batch_eligible: Optional[bool] = None, routing_snapshot: Optional[dict] = None,
                request_schema=None, request_max_tokens: Optional[int] = None,
                request_decomposition=None) -> None:
    c = dict(current())
    if batch_eligible is not None:
        c["batch_eligible"] = bool(batch_eligible)
    if intent is not None:
        c["intent"] = intent
    if measurement is not None:
        # a MEASUREMENT call (a bakeoff / priced A/B) — carried so gate._record_rt records an EXPLICIT effort
        # sentinel instead of an ambiguous NULL when the model sent no reasoning_effort, so a replicate is
        # distinguishable from an effort change in the calls corpus.
        c["measurement"] = bool(measurement)
    if gen_params is not None:
        # SCOPED determinism knobs (temperature/top_p/seed) set by `with adapters.generation(...)` — adapters.call
        # reads these as the default for every nested call so one scope governs a whole bakeoff / chat session.
        c["gen_params"] = dict(gen_params)
    if batch_expected_out is not None:
        # a caller's DECLARED per-request expected output for a batch — carried to the gate's at-create estimate so the
        # provisional cost row + the global cap use the declared basis, not the ceiling (see gate._gate_anthropic).
        c["batch_expected_out"] = batch_expected_out
    if chain is not None:
        c["chain"] = chain
    if who is not None:
        # the CALLER frame, carried across a thread boundary: a call recorded on a spawned worker/daemon walks the
        # WRONG stack (threading.py:run), so a producer that runs the real call off-thread captures caller() on the
        # calling thread and sets it here — record_call prefers it over its own (wrong-thread) stack walk.
        c["who"] = who
    if root_call is not None:
        c["root_call"] = root_call        # ROOT ledger id of this logical call — every retry's row links back (retry_of)
    if attempt is not None:
        c["attempt"] = attempt            # 1-based try number; record_call stamps retry_of/attempts from it
    if fell_from is not None:
        c["fell_from"] = fell_from        # the $0 lane a METERED fallback fell over FROM — read by the success recorder
    if fell_reason is not None:
        c["fell_reason"] = fell_reason    # the lane's OWN classified miss reason — the paid twin may itself succeed
    if call_class is not None:
        if call_class not in _CALL_CLASSES:
            raise ValueError(f"call_class must be one of {sorted(_CALL_CLASSES)}; got {call_class!r}")
        c["call_class"] = call_class
    if origin_session is not None:
        c["origin_session"] = origin_session
    if defer_batch_booking is not None:
        c["defer_batch_booking"] = bool(defer_batch_booking)
    if requested_model is not None:
        c["requested_model"] = requested_model
    if redirect_reason is not None:
        c["redirect_reason"] = redirect_reason
    if resolved_lane is not None:
        c["resolved_lane"] = resolved_lane
    if routing_snapshot is not None:
        c["routing_snapshot"] = routing_snapshot
    if request_schema is not None:
        c["request_schema"] = request_schema
    if request_max_tokens is not None:
        c["request_max_tokens"] = request_max_tokens
    if request_decomposition is not None:
        c["request_decomposition"] = request_decomposition
    _local.ctx = c                        #   (gate._record_rt fires DURING the SDK call, on this thread, so it reads ctx)


def clear_retry_context() -> None:
    """Drop the per-attempt linkage keys so a later DIRECT call on this thread never inherits a stale root_call.
    vendor_call sets them per try (propagated to the worker it runs on) and clears them when the logical call ends."""
    c = dict(current())
    c.pop("root_call", None)
    c.pop("attempt", None)
    _local.ctx = c


@contextlib.contextmanager
def fell_from_context(lane_name: str, reason=None):
    """Scope fallover provenance on the thread-local context to exactly one metered-fallback dispatch, then restore
    the prior value. The SUCCESS recorder for a metered call is gate._record_rt (the SDK-patch), which fires
    SYNCHRONOUSLY DURING the .create() call on THIS thread and reads the context via record_call — so a lane's metered
    twin, dispatched inside this block after the $0 lane went down, lands on the ledger stamped with the lane it fell
    from. Save/restore (not a bare pop) so a nested fallback never erases an outer one."""
    prev_from = current().get("fell_from")
    prev_reason = current().get("fell_reason")
    set_context(fell_from=lane_name, fell_reason=reason)
    if reason is None:
        c = dict(current())
        c.pop("fell_reason", None)
        _local.ctx = c
    try:
        yield
    finally:
        c = dict(current())
        if prev_from is None:
            c.pop("fell_from", None)
        else:
            c["fell_from"] = prev_from
        if prev_reason is None:
            c.pop("fell_reason", None)
        else:
            c["fell_reason"] = prev_reason
        _local.ctx = c


@contextlib.contextmanager
def redirect_context(requested_model: str, reason: str, resolved_lane: Optional[str] = None):
    """Carry model/lane redirect provenance through recursive calls and worker threads."""
    prev = dict(current())
    set_context(requested_model=requested_model, redirect_reason=reason, resolved_lane=resolved_lane)
    try:
        yield
    finally:
        _local.ctx = prev


@contextlib.contextmanager
def gate_internal():
    """Stamp spendguard's own meta dispatches without losing an enclosing workload intent."""
    previous = current().get("call_class")
    set_context(call_class="gate_internal")
    try:
        yield
    finally:
        restored = dict(current())
        if previous is None:
            restored.pop("call_class", None)
        else:
            restored["call_class"] = previous
        _local.ctx = restored


def recorded_intents(min_calls=1, limit=200):
    """Distinct job-type intents in the ledger with >= min_calls recorded calls, most-used first — the known label
    set an untagged prompt can be classified against (best_value's intent inference). $0, read-only; [] on any error."""
    try:
        from . import ledger_read
        rows = ledger_read.read_recorded_call_intents(min_calls, limit)
        return [r[0] for r in rows]
    except Exception:
        return []


@contextlib.contextmanager
def context(intent: Optional[str] = None, chain: Optional[str] = None, contract=None, *, batch_eligible=False):
    """`with spendguard.context(intent='loinc-typing', chain='run-42'): ...` tags the calls inside.

    On exit it emits a per-FLOW spend receipt (what ran · tokens · est→actual · running tally) — see receipt.py.
    The receipt is verbosity-gated, goes to stderr, and is fully guarded: it NEVER raises into your code.

    `batch_eligible=True` explicitly declares non-interactive, independent one-shot work; the burst guard
    offers the existing Batch dry-run/approval path. Model-specific signatures keep distinct models separate.
    `contract` gives a REALTIME loop the check a batch already gets: every response is validated against the
    declared shape as it is recorded, and the flow reports how many parsed. The batch gate could refuse before
    spending; a realtime loop cannot — the money is gone call by call — so this reports EARLY and LOUDLY rather
    than gating. The failure it catches is the one that costs: call 1 parses, call 400 comes back with a
    sentence before the JSON, and the loop keeps paying. See output_contract for the accepted forms."""
    prev = getattr(_local, "ctx", None)
    set_context(intent=intent, chain=chain, batch_eligible=batch_eligible)
    _local.ctx["bounded_structured"] = contract is not None
    prev_contract = getattr(_local, "contract", None)
    prev_tally = getattr(_local, "contract_tally", None)
    _local.contract = contract
    _local.contract_tally = {"n": 0, "parsed": 0, "salvaged": 0, "failed": 0, "first": ""}
    start = (_max_rowid(), _flow_start_usd())          # flow window: (last call rowid, billed-$ so far)
    try:
        yield
    finally:
        ctx = current()
        tally = getattr(_local, "contract_tally", None) if contract is not None else None
        _local.ctx = prev or {}
        _local.contract = prev_contract
        _local.contract_tally = prev_tally     # nested flows keep their own tally; none leaks past its block
        try:
            from . import receipt
            receipt.emit_flow(ctx.get("intent"), ctx.get("chain"), start, contract=tally)
        except Exception:
            pass


def check_output(text):
    """Validate ONE realtime response against the flow's declared contract, if any. Called by the gate as each
    call is recorded; a no-op (and never a raise) when no contract is declared — the common case."""
    c = getattr(_local, "contract", None)
    if c is None or not text:
        return
    try:
        from . import output_contract
        ok, salvaged, why = output_contract.check_item(text, c)
        t = getattr(_local, "contract_tally", None)
        if t is None:
            return
        t["n"] += 1
        if ok:
            t["parsed"] += 1
            t["salvaged"] += 1 if salvaged else 0
        else:
            t["failed"] += 1
            if not t["first"]:
                t["first"] = why
                import sys
                print(f"[spend_gate] CONTRACT FAILED on realtime call #{t['n']}: {why} — the loop is still "
                      f"spending; stop it or widen the contract.", file=sys.stderr)
    except Exception:
        pass                                           # a broken contract must never break the user's loop


def contract_tally():
    return dict(getattr(_local, "contract_tally", {}) or {})


# ── flow aggregation (powers the per-flow receipt; degrades gracefully when call-logging is off) ──
def _flow_start_usd() -> float:
    try:
        from . import budget
        return budget.spent_all_time()                     # running billed-$ to date, O(1) (memoized) — not a full re-scan
    except Exception:
        return 0.0


def _max_rowid() -> int:
    if not call_recording_consent_enabled():
        return 0
    try:
        from . import ledger_read
        # A flow watermark before the first logged call is zero; this observer never creates schema.
        from pathlib import Path
        if not Path(config.db_path()).exists() or not ledger_read.ledger_read_table_exists(ledger_read.LEDGER_CALLS_TABLE):
            return 0
        return ledger_read.read_call_max_rowid()
    except Exception:
        return 0


def flow_agg(since_rowid: int = 0, chain: Optional[str] = None):
    """Aggregate the calls logged since `since_rowid` (a flow window) → {n, in_tok, out_tok, cost, caller}, or None
    when per-call logging is off/unavailable (the receipt then falls back to the always-on budget-$ delta)."""
    if not call_recording_consent_enabled():
        return None
    try:
        from . import ledger_read
        row = ledger_read.read_call_flow(since_rowid, chain)
        if not row or not row[0]:
            return None
        return {"n": int(row[0]), "in_tok": None if row[1] is None else int(row[1]),
                "out_tok": None if row[2] is None else int(row[2]),
                "token_usage_complete": row[1] is not None and row[2] is not None,
                "cost": float(row[3] or 0.0), "caller": row[4]}
    except ledger_read.LedgerSettlementIncomplete as incomplete_flow:
        return {"n": None, "in_tok": None, "out_tok": None, "cost": None, "caller": None,
                "incomplete_count": incomplete_flow.incomplete_count,
                "input_cost_floor": incomplete_flow.input_cost_floor}
    except Exception:
        return None


def caller():
    """Return the first real application frame, never a worker-thread trampoline."""
    fr = None
    try:
        # Keep admission bounded: frame metadata avoids source lookups across the stack.
        fr = inspect.currentframe().f_back.f_back
        while fr is not None:
            fn = fr.f_code.co_filename
            base = os.path.basename(fn)
            trampoline = (base in ("thread.py", "threading.py") or
                          "concurrent/futures" in fn.replace("\\", "/"))
            if (not trampoline and not fn.startswith(_PKG) and "site-packages" not in fn
                    and fn not in ("<string>", "<stdin>")):
                return f"{os.path.basename(fn)}:{fr.f_code.co_name}:{fr.f_lineno}"
            fr = fr.f_back
    except Exception:
        pass
    finally:
        del fr
    return None


# ── storage ──
def _ensure_call_observation_schema(c):
    c.execute("SAVEPOINT call_observation_nano_schema")
    try:
        _initialize_call_observation_nano_schema(c)
        from .ledger import _initialize_spend_event_nano_schema
        _initialize_spend_event_nano_schema(c)
        c.execute("RELEASE SAVEPOINT call_observation_nano_schema")
    except BaseException:
        c.execute("ROLLBACK TO SAVEPOINT call_observation_nano_schema")
        c.execute("RELEASE SAVEPOINT call_observation_nano_schema")
        raise
    c.commit()


def _initialize_call_observation_nano_schema(c):
    """Create the calls table + its indexes + forward-only additive columns. Idempotent; run once per pooled
    connection by config.pooled_ledger_conn."""
    c.execute("""CREATE TABLE IF NOT EXISTS calls(
        id TEXT PRIMARY KEY, ts TEXT, chain TEXT, intent TEXT, caller TEXT,
        provider TEXT, model TEXT, kind TEXT,
        in_tok INTEGER, out_tok INTEGER, cost REAL, latency REAL,
        prompt_hash TEXT, prompt_snip TEXT, output_snip TEXT, finish TEXT,
        quality TEXT, quality_src TEXT, quality_conf REAL,
        executor TEXT, project TEXT, effort TEXT, suspect TEXT,
        call_class TEXT, origin_session TEXT,
        requested_model TEXT, served_model TEXT, resolved_lane TEXT, redirect_reason TEXT,
        cache_read_tok INTEGER, cache_write_tok INTEGER)""")
    c.execute("CREATE INDEX IF NOT EXISTS idx_calls_chain ON calls(chain)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_calls_intent ON calls(intent)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_calls_ts ON calls(ts)")  # as_of/since range reads (calibrate, advise)
    # Migrate older dbs: add every column the schema gained after they were created. Column names are
    # fixed literals from this tuple (never caller input) — SQLite cannot parameterize a DDL identifier,
    # so the f-string is the only way and carries no injection surface. `executor` = which subscription
    # lane served the call; `project` = the repo it belongs to (so lane plan-value attributes like spend).
    _have = {r[1] for r in c.execute("PRAGMA table_info(calls)").fetchall()}
    missing_nano_columns = set(CALL_OBSERVATION_NANO_COLUMNS.values()) - _have
    # `effort` = the reasoning-effort TIER actually sent (none|minimal|low|medium|high|… or the wire
    # value a model accepts), so cost×quality can be sliced per (intent, model, EFFORT) — the axis the
    # best-value selector titrates. NULL on a non-reasoning call, a call that sent no effort, or a legacy row.
    # `suspect` = a data-integrity marker: a per-call out_tok that EXCEEDS the model's real output ceiling is
    # physically impossible for one call (measured: backfill/aggregate rows recorded 1M-68M out_tok under gpt-5-nano,
    # whose ceiling is 128K), so it is flagged (never trusted as a per-call fact, excluded from per-call analysis) but
    # KEPT (raw value preserved — never a silent clamp/delete). NULL on a clean row.
    #
    # FORENSIC OUTCOME COLUMNS (the accounting mandate: EVERY call is on the ledger, success AND failure — a spend
    # tool that drops failures cannot answer "what failed and why", which is the whole point of the tool). These
    # carry the FULL disposition of a call that did not simply succeed, so reliability is a queryable fact, never a
    # grep of a flat log or a lost error. All NULL on a legacy row or a plain success:
    #   `outcome`        = the vendor_call KIND taxonomy (ok|overloaded|transport_error|deadline_exceeded|
    #                      payload_rejected|refused|truncated|empty|schema_violation|unfunded|preflight_unmet) —
    #                      one shared vocabulary with vendor_call, never a second taxonomy that could drift.
    #   `http_status`    = the provider's HTTP status (429/529 overload vs 400/413 rejection) — the axis that tells
    #                      "pace + retry" from "never retry". Extracted structurally (_exc_detail), never from prose.
    #   `provider_error` = the provider's real error BODY (the reason lives there, not in the one-line str(e)).
    #   `retry_after`    = the seconds the provider told us to wait on a 429/529 (its Retry-After) — so pacing can be
    #                      proven to honor it.
    #   `attempts`       = how many tries the call took before this outcome (1 = first-try; N = retried N-1 times).
    #   `disposition`    = the terminal fate a caller saw: served | served_after_retry | failed_over (to another
    #                      path/vendor) | failed (exhausted). This is what a reliability report counts.
    #   `retry_of`       = the ROOT call id this row is a retry of (NULL on the first attempt). EVERY retry is its OWN
    #                      row pointing back to the original, so the entire attempt history of one logical call —
    #                      first try + every retry, on whatever provider/lane/meter each landed — is reconstructable
    #                      by `WHERE id=X OR retry_of=X ORDER BY ts`. Attempts are never collapsed into one row.
    #   `fell_from`      = the $0 SUBSCRIPTION LANE this METERED call fell over FROM, when a down/unsuitable lane forced
    #                      the paid twin (the mirror of `executor`, which names the lane that SERVED a $0 hit).
    #   `fell_reason`    = that lane's OWN classified failure reason. This is separate because the metered twin may
    #                      succeed, leaving its own outcome/provider_error unable to explain the fallover.
    #                      `SUM(cost) WHERE fell_from='codex'` is then
    #                      the metered $ this install spent BECAUSE codex was down — the "$0 savings lost while a lane was
    #                      down" an operator restores by re-logging that lane in. NULL on a $0-lane hit or a direct call.
    for _col, _decl in (("quality_conf", "REAL"), ("executor", "TEXT"), ("project", "TEXT"),
                        ("effort", "TEXT"), ("suspect", "TEXT"),
        ("settlement_complete", "INTEGER"), ("input_cost_floor", "REAL"),
        ("token_usage_complete", "INTEGER"),
                        ("outcome", "TEXT"), ("http_status", "INTEGER"), ("provider_error", "TEXT"),
                        ("retry_after", "REAL"), ("attempts", "INTEGER"), ("disposition", "TEXT"),
                        ("disposition_reason", "TEXT"),
                        ("retry_of", "TEXT"), ("fell_from", "TEXT"), ("fell_reason", "TEXT"),
                        ("call_class", "TEXT"), ("origin_session", "TEXT"),
                        ("requested_model", "TEXT"), ("served_model", "TEXT"),
                        ("resolved_lane", "TEXT"), ("redirect_reason", "TEXT"),
                        ("cache_read_tok", "INTEGER"), ("cache_write_tok", "INTEGER"),
                        ("routing_snapshot_json", "TEXT"), ("request_schema_json", "TEXT"),
                        ("request_max_tokens", "INTEGER"), ("request_decomposition_json", "TEXT"),
                        *((column, "INTEGER") for column in CALL_OBSERVATION_NANO_COLUMNS.values())):
        if _col not in _have:
            c.execute(f"ALTER TABLE calls ADD COLUMN {_col} {_decl}")
    if missing_nano_columns:
        _backfill_call_observation_nano_columns(c, missing_nano_columns)
    # Completeness is orthogonal to success/failure. NULL denotes unverifiable legacy
    # provenance; never infer historical completeness from a zero token count.
    c.execute("CREATE INDEX IF NOT EXISTS idx_calls_executor ON calls(executor)")  # per-lane rollups
    c.execute("CREATE INDEX IF NOT EXISTS idx_calls_outcome ON calls(outcome)")    # reliability / error-class rollups
    c.execute("CREATE INDEX IF NOT EXISTS idx_calls_retry_of ON calls(retry_of)")  # reconstruct one call's retry chain
    c.execute("CREATE INDEX IF NOT EXISTS idx_calls_fell_from ON calls(fell_from)")  # per-lane fallback-spend rollups
    c.execute("CREATE INDEX IF NOT EXISTS idx_calls_incomplete_settlement ON calls(settlement_complete) "
              "WHERE settlement_complete=0")
    c.execute("CREATE INDEX IF NOT EXISTS idx_calls_class_session ON calls(call_class, origin_session)")
    ensure_ordered_index_coverage(c, "calls", ("kind", "ts"))
    ensure_ordered_index_coverage(c, "calls", ("kind", "provider", "model", "intent"))
    ensure_ordered_index_coverage(c, "calls", ("finish", "ts"))
    from . import routing_audit
    routing_audit.ensure_decisions_call_fk_schema(c, commit=False)
    # Default-on complete content retention shares this connection so large governed fans do not
    # consume a second per-thread ledger connection. Its schema/purge seam is additive and local-only.


def _calls_db():
    """The calls table, on the shared pooled ledger connection (config.pooled_ledger_conn, keyed 'calls') — reused,
    tuned, fork-safe. `_lock` still serializes the module's read-modify-write sites."""
    return config.pooled_ledger_conn("calls", _ensure_call_observation_schema)


def _uuid():
    import uuid
    return uuid.uuid4().hex[:16]


def _resolve_attribution(model, cost, intent, chain, project, call_class=None, origin_session=None):
    """The SHARED attribution brain for EVERY writer into the `calls` table (record_call AND insert), so no INSERT
    path can drift un-attributed — the record-call-outcome DRIFT the capability map found (insert was a second,
    ungated, un-attributed INSERT). Two things, one place:
      1. ENFORCE that a PAID call carries an intent — an un-intented paid row lands in '(none)', invisible to
         advise/denylists/rollups (judge-class denylist entries matched zero rows for weeks). Raise under
         SPENDGUARD_REQUIRE_INTENT=1, else a warn via the stdlib registry. Runs BEFORE any enabled()/try, so the
         raise cannot be swallowed. The SAFETY half (un-intented → refuse substitution) already holds elsewhere.
      2. RESOLVE intent/chain from the live context and the project from the repo (same as the money ledger).
    Returns (intent, chain, project_lc)."""
    if not (intent or (current() or {}).get("intent")) and float(cost or 0) > 0:
        import os as _osw
        _msg = ("a PAID call (%s, $%.4f) with NO intent → would attribute to '(none)'. Tag it via "
                "calls.set_context(intent=…) or adapters.call(sig=…) so spend/advise/denylists can see it."
                % (model, float(cost or 0)))
        if _osw.getenv("SPENDGUARD_REQUIRE_INTENT") == "1":
            raise ValueError("[spendguard] " + _msg + " (SPENDGUARD_REQUIRE_INTENT=1)")
        import warnings as _warnings
        _warnings.warn("[spendguard] " + _msg + " (SPENDGUARD_REQUIRE_INTENT=1 to enforce)", stacklevel=2)
    ctx = current()
    intent = intent or ctx.get("intent")
    chain = chain or ctx.get("chain")
    if project is None:                                  # attribute to the repo the same way the money ledger does
        try:
            from . import budget
            project = budget._project()
        except Exception:
            project = None
    resolved_class = call_class or ctx.get("call_class") or "workload"
    if resolved_class not in _CALL_CLASSES:
        raise ValueError(f"call_class must be one of {sorted(_CALL_CLASSES)}; got {resolved_class!r}")
    resolved_session = origin_session or ctx.get("origin_session") or _ORIGIN_SESSION
    from .budget import normalize_ledger_project_id
    return (intent, chain, (normalize_ledger_project_id(project) or None), resolved_class,
            resolved_session)   # lowercased project_primary for clean joins


# A clean per-call out_tok is <= the model's output ceiling (the API caps completion at max_tokens <= ceiling), so
# out_tok above ceiling x this factor is physically impossible for ONE call (an aggregate/backfill/bug). The factor is
# > 1 only to absorb curated-ceiling / token-count slop; it never false-flags a legit at-ceiling output.
_SUSPECT_CEILING_FACTOR = 1.5

# The last-resort out_tok sanity bound when the catalog knows NEITHER an output ceiling NOR a context window for a model
# (an uncurated / newly-seen id). No single completion emits this many tokens today — the largest published output
# ceiling is well under 1M — so a per-call out_tok above this is definitionally an aggregate/backfill/recording bug (the
# measured 1M-68M gpt-5-nano rows). Deliberately generous so it never false-flags a real call, only catches gross bugs.
_UNIVERSAL_OUT_TOK_SANITY = 1_000_000


def _call_settlement_values(provider, model, kind, cost, in_tok, out_tok,
                            cache_read_tok=None, cache_write_tok=None, input_cost_floor=None):
    """Resolve independent monetary and token completeness for every call-table writer.

    A non-None cost is an authoritative total, including a genuine zero; an input-only
    floor belongs in input_cost_floor and must never be supplied as cost.
    Legacy rows are deliberately not migrated: a historical zero has lost its provenance.
    Prices come exclusively from pricing; an unpriceable floor stays unknown, never zero.
    """
    token_usage_complete = in_tok is not None and out_tok is not None
    # `cost` is a total, never an input-only floor. Monetary evidence is independent
    # of token telemetry for EVERY writer (API, subscription, batch and backfill).
    settlement_complete = cost is not None
    if not settlement_complete and input_cost_floor is None and in_tok is not None:
        try:
            from . import pricing
            if kind == "subscription":
                input_cost_floor = 0.0
            else:
                input_cost_floor = pricing.input_cost_lower_bound(
                    model, in_tok, batch=kind == "batch", provider=provider,
                    cache_read_tok=cache_read_tok, cache_write_tok=cache_write_tok)
        except (KeyError, TypeError, ValueError) as floor_error:
            config.warn_once(f"[spendguard] incomplete settlement: input floor unavailable for {model}: {floor_error}")
    if not settlement_complete:
        config.warn_once("[spendguard] incomplete call settlement: total cost unreported; "
                         "known input floor retained, total remains unknown")
    return (int(in_tok) if in_tok is not None else None,
            int(out_tok) if out_tok is not None else None,
            float(cost) if settlement_complete else None,
            int(settlement_complete), input_cost_floor, int(token_usage_complete))


def _out_tok_sanity_ceiling(model):
    """The bound a single call's out_tok cannot legitimately exceed, resolved so the suspect check is NEVER BLIND (the
    gap that let deepseek's 393,216-token artifact and the 68M-token rows through when a model had no curated ceiling):
      1. the curated published OUTPUT ceiling (the tight, correct bound), else
      2. the context WINDOW (a call cannot output more tokens than the window holds), else
      3. _UNIVERSAL_OUT_TOK_SANITY (no call today emits this many — anything above is a bug, whatever the model).
    Returns (bound:int, basis:str). Never raises; a catalog hiccup degrades to the universal bound, never to 'no bound'."""
    from . import model_catalog as _mc
    try:
        v = _mc.published_ceiling(model)
        if v:
            return int(v), "output_ceiling"
        cw = _mc.context_window(model)
        if cw:
            return int(cw), "context_window"
    except Exception:
        pass
    return _UNIVERSAL_OUT_TOK_SANITY, "universal_bound"


def serialize_call_observation_mutation(function=None, *, capture_application_caller=False):
    """Carry caller-local attribution into the serialized database mutation."""
    import functools
    if function is None:
        return lambda target: serialize_call_observation_mutation(
            target, capture_application_caller=capture_application_caller)
    signature = inspect.signature(function)
    @functools.wraps(function)
    def invoke_call_observation_mutation(*args, **kwargs):
        from .ledger_writer import run_ledger_write, current_ledger_writer_connection
        if current_ledger_writer_connection() is not None:
            return run_ledger_write(lambda: function(*args, **kwargs))
        if capture_application_caller:
            bound = signature.bind_partial(*args, **kwargs)
            if bound.arguments.get("who") is None:
                bound.arguments["who"] = current().get("who") or caller()
                args, kwargs = bound.args, bound.kwargs
        caller_state = vars(_local).copy()
        def apply_call_observation_mutation():
            previous_state = vars(_local).copy()
            vars(_local).clear()
            vars(_local).update(caller_state)
            try:
                return function(*args, **kwargs)
            finally:
                vars(_local).clear()
                vars(_local).update(previous_state)
        return run_ledger_write(apply_call_observation_mutation)
    return invoke_call_observation_mutation


@serialize_call_observation_mutation(capture_application_caller=True)
def record_call(provider, model, kind, cost, in_tok=0, out_tok=0, latency=None,
           prompt=None, output=None, finish=None, intent=None, chain=None, who=None,
           executor=None, project=None, effort=None, *, outcome=None, http_status=None,
           provider_error=None, retry_after=None, attempts=None, disposition=None, disposition_reason=None, retry_of=None,
           fell_from=None, fell_reason=None, call_id=None, call_class=None, origin_session=None,
           requested_model=None, served_model=None, resolved_lane=None, redirect_reason=None,
           cache_read_tok=None, cache_write_tok=None, routing_snapshot=None, request_schema=None,
           request_max_tokens=None, request_decomposition=None, input_cost_floor=None):
    """Record one call's full outcome, success OR failure, and return call_id.

    Outcome failures never raise merely because the provider call failed;
    persistence failures propagate honestly rather than claiming a recorded call.

    Cache observations remain NULL when unmeasured or when a write tier is unknown.
    Complete split observations use input inclusive of reads, excluding writes; writes identify the five-minute tier.

    FORENSIC MANDATE (the reason this tool exists): EVERY call is on the ledger, whatever its fate — a metered success,
    a $0 lane hit, a 429 overload, a transport drop, a schema miss, an exhausted-after-N-retries failure. So the OUTCOME
    METADATA is written ALWAYS, independent of the privacy opt-in; only the prompt/output CONTENT snippets stay gated by
    the opt-in (they can carry sensitive text). A spend/accounting tool that silently dropped failures could never
    answer "what failed, why, and how often" — which is the whole question. (`SPENDGUARD_NO_LEDGER=1` is the explicit
    escape hatch for the rare caller that truly wants zero rows; the gate's GATE_DISABLE/`spendguard off` still applies.)

    Forensic fields (all NULL on a legacy row or a plain first-try success):
      outcome        — the shared vendor_call KIND (ok|overloaded|transport_error|deadline_exceeded|payload_rejected|
                       refused|truncated|empty|schema_violation|unfunded|preflight_unmet). One vocabulary, no drift.
      http_status    — provider HTTP status (429/529 vs 400/413), extracted structurally, never from prose.
      provider_error — the provider's real error BODY (capped), the reason that does not live in str(e).
      retry_after    — seconds the provider asked us to wait (its Retry-After) — so pacing can be proven to honor it.
      attempts       — how many tries produced THIS row (1 = first).
      disposition    — served | served_after_retry | failed_over | failed (exhausted). What a reliability report counts.
      retry_of       — the ROOT call id this row is a retry of (NULL on the first try). Every retry is its own row.
      fell_from      — the $0 lane this METERED call fell over FROM (a down/unsuitable lane forced the paid twin); the
                       mirror of `executor`. A plain fact, never a why. Passed explicitly by the failure recorder, or
                       read from the thread-local context (set by calls.fell_from_context) for the success recorder.
      fell_reason    — the lane's OWN classified miss reason; separate from the metered twin's provider_error, which
                       is NULL when that fallback succeeds. Read from the same scoped context when not explicit.

    `executor` names the SUBSCRIPTION LANE that served the call (claude-code / codex / gemini / zai-coding) when it
    rode a flat-fee plan instead of the metered API. Storing it makes "which lane worked" a recorded fact the receipt
    can show and the lane est-value stamper can price — rather than a guess inferred from the provider. `project` is
    the repo the call belongs to (derived from the live gate context when not passed), so a lane's plan VALUE
    attributes to a project exactly like billed spend does."""
    # ATTRIBUTION is resolved by the SHARED brain _attribute (enforce a paid un-intented call + resolve intent/chain/
    # project) BEFORE call_recording_consent_enabled()/try, so record_call and insert can never drift on it (the record-call-outcome DRIFT).
    intent, chain, proj, call_class, origin_session = _resolve_attribution(
        model, cost, intent, chain, project, call_class, origin_session)
    if config.is_explicit_true(os.getenv("SPENDGUARD_NO_LEDGER")):
        return None                                      # explicit total opt-out (rare) — otherwise ALWAYS record
    # The OUTCOME row is recorded ALWAYS (forensic mandate); `call_recording_consent_enabled()` + store_prompts now gate ONLY the private
    # CONTENT snippets, never whether the call is on the ledger. A failure with the opt-in off MUST still land.
    _content_ok = call_recording_consent_enabled() and _store_prompts()
    try:
        ctx = current()                                  # for the `who` fallback + per-attempt retry linkage below
        # PER-ATTEMPT LINKAGE (Ash: "each retry a row pointing to the original"): vendor_call puts the logical call's
        # root_call id + 1-based attempt into the context before each try (propagated to the worker this runs on), so the
        # whole retry chain of ONE call reconstructs as `WHERE id=root OR retry_of=root ORDER BY ts`. An EXPLICIT arg
        # from the caller always wins; a direct single-shot call (no root in context) is its own root, unlinked.
        _root, _seq = ctx.get("root_call"), ctx.get("attempt")
        if retry_of is None and _root and isinstance(_seq, int) and _seq > 1:
            retry_of = _root
        if attempts is None and isinstance(_seq, int):
            attempts = _seq
        # The metered-fallback SUCCESS recorder (gate._record_rt) passes no fell_from arg — it fires during the SDK
        # call on this thread, so read the lane from the context calls.fell_from_context scoped around that dispatch.
        if fell_from is None:
            fell_from = ctx.get("fell_from")
        if fell_reason is None:
            fell_reason = ctx.get("fell_reason")
        requested_model = requested_model or ctx.get("requested_model")
        served_model = served_model or model
        resolved_lane = resolved_lane or ctx.get("resolved_lane") or executor
        redirect_reason = redirect_reason or ctx.get("redirect_reason")
        routing_snapshot = routing_snapshot if routing_snapshot is not None else ctx.get("routing_snapshot")
        request_schema = request_schema if request_schema is not None else ctx.get("request_schema")
        request_max_tokens = (request_max_tokens if request_max_tokens is not None
                              else ctx.get("request_max_tokens"))
        request_decomposition = (request_decomposition if request_decomposition is not None
                                 else ctx.get("request_decomposition"))
        cid = call_id or (_root if (_root and _seq == 1) else None) or _uuid()
        sp = _snip()
        ph = hashlib.sha256((prompt or "").encode("utf-8", "ignore")).hexdigest()[:16] if prompt else None
        psnip = prompt[:sp] if (prompt and _content_ok) else None
        osnip = output[:sp] if (output and _content_ok) else None
        ts = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")
        # DATA-INTEGRITY: a per-call out_tok above the model's real output ceiling is physically impossible for ONE
        # call — an aggregate/backfill row or a recording bug (measured: 1M-68M out_tok under gpt-5-nano, ceiling 128K).
        # FLAG it (the raw value is KEPT — never a silent clamp/delete) so per-call analysis can exclude it; it is not a
        # trustworthy per-call fact. The bound is ceiling x _SUSPECT_CEILING_FACTOR: a clean call is <= the ceiling, so
        # the factor only absorbs curation/token-count slop and never false-flags a legit at-ceiling output.
        suspect = None
        settlement_values = _call_settlement_values(provider, model, kind, cost, in_tok, out_tok,
                                                    cache_read_tok, cache_write_tok, input_cost_floor)
        try:
            _ceil, _basis = _out_tok_sanity_ceiling(model)   # NEVER None — output_ceiling → context_window → universal
            if _ceil and int(out_tok or 0) > int(_ceil) * _SUSPECT_CEILING_FACTOR:
                suspect = (f"out_tok {int(out_tok or 0)} > {_SUSPECT_CEILING_FACTOR}x {_basis} {int(_ceil)} "
                           f"(impossible per-call: aggregate/backfill/bug)")
                config.warn_once(f"[spendguard] calls: suspect per-call out_tok {int(out_tok or 0)} for {model} "
                                 f"({_basis} {int(_ceil)}) — flagged, not trusted as a per-call fact")
        except Exception as _sce:
            from . import gate as _scg
            if _scg.is_deliberate_stop(_sce):    # a spend/deadline/containment stop HALTS — never swallowed into a flag
                raise
            suspect = None                       # a pure catalog-read hiccup never blocks recording the call
        with _lock:
            # A provider-declared cutoff is authoritative per-call quality evidence. Assign it only while the new
            # row is written (never migrate/backfill old rows), with a distinct source protected from later feedback.
            from . import bulkgate
            _truncation_quality = bulkgate.is_truncated(finish)
            from . import routing_audit
            audit_connection = _calls_db()
            audit_connection.execute(
                "INSERT INTO calls (id,ts,chain,intent,caller,provider,model,kind,in_tok,out_tok,"
                "cost,latency,prompt_hash,prompt_snip,output_snip,finish,executor,project,effort,suspect,"
                "quality,quality_src,quality_conf,outcome,http_status,provider_error,retry_after,attempts,disposition,disposition_reason,retry_of,fell_from,fell_reason,"
                "call_class,origin_session,requested_model,served_model,resolved_lane,redirect_reason,cache_read_tok,cache_write_tok,"
                "routing_snapshot_json,request_schema_json,request_max_tokens,request_decomposition_json,"
                "settlement_complete,input_cost_floor,token_usage_complete,cost_nano,input_cost_floor_nano) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (cid, ts, chain, intent, who or ctx.get("who") or caller(), provider, model, kind,
                 *settlement_values[:3], latency, ph, psnip, osnip, finish,
                 executor, proj, (effort or None), suspect,
                 ("bad" if _truncation_quality else None), ("truncation" if _truncation_quality else None),
                 (1.0 if _truncation_quality else None),
                 (outcome or None),
                 (int(http_status) if str(http_status or "").strip().isdigit() else None),
                 (provider_error[:1000] if isinstance(provider_error, str) else None),
                 (float(retry_after) if isinstance(retry_after, (int, float)) else None),
                 (int(attempts) if isinstance(attempts, int) else None),
                 (disposition or None), (disposition_reason or None), (retry_of or None), (fell_from or None),
                 (fell_reason or None), call_class, origin_session,
                 (requested_model or None), (served_model or None), (resolved_lane or None),
                 (redirect_reason or None),
                 (int(cache_read_tok) if cache_read_tok is not None else None),
                 (int(cache_write_tok) if cache_write_tok is not None else None),
                 routing_audit.encode_audit_json(routing_snapshot),
                 routing_audit.encode_audit_json(request_schema),
                 (int(request_max_tokens) if request_max_tokens is not None else None),
                 routing_audit.encode_audit_json(request_decomposition), *settlement_values[3:],
                 *_call_observation_nano_amounts(settlement_values[2], settlement_values[4])))
            routing_audit.insert_linked_decision(
                audit_connection,
                call_id=cid,
                project=proj,
                intent=intent,
                basis=(routing_snapshot or {}).get("basis", routing_audit.DIRECT_ROUTING_BASIS),
                requested_model=requested_model or model,
                requested_effort=effort,
                chosen_model=served_model,
                chosen_effort=effort,
                counterfactual_usd=settlement_values[2],
                actual_usd=settlement_values[2],
                saved_usd=0.0 if settlement_values[3] else None,
                why=redirect_reason or routing_audit.DIRECT_ROUTING_REASON,
                reason_class=(routing_snapshot or {}).get(
                    "reason_class", routing_audit.DIRECT_ROUTING_REASON_CLASS),
            )
            audit_connection.commit()
        # Reuse alone must leave quality unjudged; the compatibility entrypoint never infers a verdict.
        if chain and prompt:
            _link_used(chain, prompt)
        return cid
    except Exception:
        config.rollback_ledger_conn("calls")   # a failed write left a txn open on the REUSED pooled conn holding the
        raise



_CONF = {"explicit": 1.0, "truncation": 1.0, "judge": 0.95, "used": 0.6, "mined": 0.5}


@serialize_call_observation_mutation
def feedback(call_id: Optional[str], ok: bool = True, source: str = "explicit",
             confidence: Optional[float] = None) -> None:
    """Label a call's quality after the fact (judge verdict, human accept, downstream validation).
    Carries a confidence (explicit=1.0, judge=0.95, used=0.6, mined=0.5) the advisor weights by."""
    if not call_id:
        return
    conf = confidence if confidence is not None else _CONF.get(source, 0.7)
    try:
        with _lock:
            _calls_db().execute("UPDATE calls SET quality=?, quality_src=?, quality_conf=? WHERE id=? AND COALESCE(quality_src,'') != 'truncation'",
                          ("good" if ok else "bad", source, conf, call_id))
            _calls_db().commit()
    except Exception:
        config.rollback_ledger_conn("calls")   # clear a dangling txn from the failed write on the reused pooled conn
        raise


@serialize_call_observation_mutation
def finalize_bakeoff_measurement(call_id, provider, requested_model, served_model, cost, in_tok, out_tok, intent,
                                 quality, effort, resolved_lane, redirect_reason=None):
    """Attach a bakeoff verdict to its gate-recorded physical call, inserting only when no recorder produced a row."""
    confidence = _CONF["judge"] if quality is not None else None
    with _lock:
        con = _calls_db()
        changed = con.execute(
            "UPDATE calls SET quality=?, quality_src=?, quality_conf=?, caller='bakeoff', "
            "requested_model=COALESCE(requested_model,?), served_model=COALESCE(served_model,?), "
            "resolved_lane=COALESCE(resolved_lane,?), redirect_reason=COALESCE(redirect_reason,?) WHERE id=?",
            (quality, "judge" if quality is not None else None, confidence, requested_model, served_model,
             resolved_lane, redirect_reason, call_id)).rowcount
        con.commit()
    if changed:
        return call_id
    return record_call(provider, served_model, "realtime", cost, in_tok=in_tok, out_tok=out_tok, intent=intent,
                       who="bakeoff", effort=effort, call_id=call_id, requested_model=requested_model,
                       served_model=served_model, resolved_lane=resolved_lane, redirect_reason=redirect_reason)


@serialize_call_observation_mutation
def insert(provider, model, kind, cost, in_tok=0, out_tok=0, ts=None, intent=None, chain=None,
           quality=None, quality_src=None, quality_conf=None, who="backfill", effort=None, project=None,
           call_class=None, origin_session=None):
    """Low-level insert used by backfill (ungated) and the bakeoff/titration (per-EFFORT arms, with an inline quality
    label). Returns call_id. Shares the ATTRIBUTION brain (_attribute) with record_call — so a paid row here enforces
    intent + records the project exactly like a live call, never a second un-attributed INSERT (the
    record-call-outcome DRIFT). `effort` = the reasoning tier this row was produced at (sliceable evidence)."""
    intent, chain, proj, call_class, origin_session = _resolve_attribution(
        model, cost, intent, chain, project, call_class, origin_session)
    cid = _uuid()
    ts = ts or datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")
    settlement_values = _call_settlement_values(provider, model, kind, cost, in_tok, out_tok)
    with _lock:
        _calls_db().execute(
            "INSERT INTO calls (id,ts,chain,intent,caller,provider,model,kind,in_tok,out_tok,"
            "cost,quality,quality_src,quality_conf,effort,project,call_class,origin_session,"
            "settlement_complete,input_cost_floor,token_usage_complete,cost_nano,input_cost_floor_nano) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (cid, ts, chain, intent, who, provider, model, kind,
             *settlement_values[:3], quality, quality_src, quality_conf,
             (effort or None), proj, call_class, origin_session, *settlement_values[3:],
             *_call_observation_nano_amounts(settlement_values[2], settlement_values[4])))
        _calls_db().commit()
    return cid


@serialize_call_observation_mutation
def reconcile_calls_batch_estimate(provider, model, intent, in_tok, out_tok, cost):
    """Revise the newest matching provisional call row to provider-measured batch usage, preserving the row."""
    with _lock:
        con = _calls_db()
        row = con.execute(
            "SELECT id FROM calls WHERE kind='batch' AND provider=? AND model=? AND intent IS ? "
            "ORDER BY rowid DESC LIMIT 1", (provider, model, intent)).fetchone()
        if not row:
            return False
        settlement_values = _call_settlement_values(provider, model, "batch", cost, in_tok, out_tok)
        con.execute("UPDATE calls SET in_tok=?, out_tok=?, cost=?, settlement_complete=?, input_cost_floor=?, "
                    "token_usage_complete=?, cost_nano=?, input_cost_floor_nano=? WHERE id=?",
                    (*settlement_values, *_call_observation_nano_amounts(settlement_values[2], settlement_values[4]), row[0]))
        con.commit()
    return True


def _link_used(chain, current_prompt):
    """Leave reuse unjudged (quality=NULL) until explicit feedback or an agentic judge labels it.

    Kept as the implicit-reuse entrypoint for compatibility. Output appearing in a later prompt is not evidence
    that it was correct or usable: it may have been quoted for correction. Do not infer reuse or quality from
    stored snippets, and never replace an existing measured verdict. NULL is the corpus's unjudged marker;
    cost_summary and advise.evidence exclude it from their good/bad and confidence-weighted label counts.
    """
    return None


def intent_spend(intent, window_s=86400):
    """The ACTUAL metered $ recorded for `intent` in the last `window_s` seconds — what guardrail D's per-intent
    running cap (enforced at the call door, gate._rt_precheck_usd) sums to decide whether the next call would cross the
    ceiling. Rolling window; ts is ISO-8601 UTC so a lexical `>=` is chronological. Returns 0.0 on empty;
    errors and incomplete settlements refuse rather than disable the cap. This is the path-independent twin of
    bulk_delegate's in-fan budget_usd: every metered call reaches the door, however it was issued."""
    if not intent:
        return 0.0
    import datetime
    cut = (datetime.datetime.now(datetime.timezone.utc)
           - datetime.timedelta(seconds=max(1, int(window_s)))).isoformat(timespec="seconds")
    try:
        from . import ledger_read
        connection = _calls_db()
        available_columns = {column[1] for column in connection.execute("PRAGMA table_info(calls)")}
        incomplete = ledger_read._read_incomplete_settlement_totals(
            connection, since=cut, intent=intent)
        if incomplete["n"]:
            raise ledger_read.LedgerSettlementIncomplete(
                incomplete["n"], incomplete["input_cost_floor"])
        legacy_cost_column, nano_cost_column = next(iter(CALL_OBSERVATION_NANO_COLUMNS.items()))
        selected_cost_column = ledger_read._prefer_nano_else_legacy_column(
            available_columns, nano_cost_column, legacy_cost_column)
        if selected_cost_column is None:
            raise RuntimeError("Calls ledger has no cost column")
        if selected_cost_column == nano_cost_column:
            cost_expression = (
                f"COALESCE({nano_cost_column}, ROUND({legacy_cost_column} * {NANO_USD_PER_USD}))")
            total_expression = f"COALESCE(SUM({cost_expression}),0)/{NANO_USD_PER_USD}.0"
        else:
            total_expression = f"COALESCE(SUM({legacy_cost_column}),0)"
        total = connection.execute(
            f"SELECT {total_expression} FROM calls WHERE intent=? AND ts>=?", (intent, cut)).fetchone()[0]
        return float(total)
    except ledger_read.LedgerSettlementIncomplete:
        raise
    except Exception:
        config.rollback_ledger_conn("calls")
        raise  # an unknown settlement must not disable the running monetary cap


def cost_summary(intent=None):
    """Per (intent, model): calls, $ total, %good, and cost-per-good-result."""
    from . import ledger_read
    return [row.values for row in ledger_read.read_call_cost_summary(intent)]


def mean_out_by_executor_model(intent, call_class=None, origin_session=None):
    """{(executor_or_provider, model): {'mean_out', 'n'}} for an intent — the measured OUTPUT norm per lane-arm. out_tok
    INCLUDES reasoning tokens (reasoning bills as output), so a model that OVER-REASONS on this intent shows a genuinely
    larger mean here — which is what lets the bandit price its REALIZED cost and deprioritize it. `executor` is the
    subscription LANE for a lane-served call (else the provider), matching how the bandit keys an arm. Excludes rows
    with no out_tok AND rows flagged `suspect` (a per-call out_tok that EXCEEDS the model's ceiling — an aggregate/
    backfill/bug value that is physically impossible for one call; the schema keeps the raw value but marks it
    'exclude from per-call analysis'). Leaving them in would let a single 2.8M-tok artifact row dominate the mean and
    make the value judge wrongly price an arm as hugely expensive — the same contamination reconcile_calls already
    excludes with `suspect IS NULL`. {} on any error (never raises — a routing read must not break routing)."""
    out = {}
    if not intent:
        return out
    try:
        from . import ledger_read
        rows = ledger_read.read_call_output_means(intent, call_class, origin_session)
        for row in rows:
            ex, model, avg_out, n = row.values
            out[(ex or "?", model or "?")] = {"mean_out": None if avg_out is None else float(avg_out), "n": int(n)}
    except Exception:
        pass
    return out


def lane_model_outcomes(executor, since=None, call_class=None):
    """{model: {"ok", "fail", "last_ok", "last_fail", "by_outcome"}} for ONE lane (executor), grouped by model — the
    MEASURED served/rejected evidence lane_servability reads to decide which models a lane ACTUALLY serves. Never
    prose-parsed: a model the lane never succeeds on but repeatedly fails is a rejection established by the PATTERN
    (zero ok, sustained fail), not by reading an error string. `since` (ISO8601 UTC) bounds the window; None = all
    history. A DELIBERATE gate refusal is not a servability signal (the lane could have served it — the spend gate
    said no), so it is counted in neither ok nor fail. {} on any error — a routing read must never raise."""
    out = {}
    if not executor:
        return out
    try:
        from . import ledger_read
        rows = ledger_read.read_call_lane_outcomes(executor, since, call_class)
        for row in rows:
            model, outcome, n, last_ts = row.values
            rec = out.setdefault(model or "?", {"ok": 0, "fail": 0, "last_ok": None,
                                                "last_fail": None, "by_outcome": {}})
            n = int(n or 0)
            rec["by_outcome"][outcome or ""] = rec["by_outcome"].get(outcome or "", 0) + n
            if outcome == "ok":
                rec["ok"] += n
                if last_ts and (rec["last_ok"] is None or last_ts > rec["last_ok"]):
                    rec["last_ok"] = last_ts
            elif outcome == "gate_refused":
                pass                                      # deliberate spend-gate refusal — not a lane capability signal
            elif outcome:                                 # any other recorded non-ok outcome is a served-attempt that missed
                rec["fail"] += n
                if last_ts and (rec["last_fail"] is None or last_ts > rec["last_fail"]):
                    rec["last_fail"] = last_ts
            # outcome == '' (unsettled / pre-taxonomy row) counts toward neither — it is not evidence either way
    except Exception:
        pass
    return out


def metered_realtime_by_intent(since):
    """[{intent, model, calls, usd, in_tok, avg_in}] for REALTIME METERED spend since `since` (ISO8601 UTC), grouped
    by (intent, model), biggest $ first. The corpus lane_eligibility judges: which of this BILLED realtime work was
    independent one-shot comprehension that could have ridden a $0 lane or the Batch API. Metered = cost>0 AND no $0
    lane executor; realtime-only so the Batch-API paths (legitimately metered, half price) are excluded by
    construction; embeddings are excluded (kind!='realtime'); spendguard's own meta calls are excluded. {} on error —
    a reporting read must never raise."""
    out = []
    if not since:
        return out
    try:
        from . import ledger_read
        rows = ledger_read.read_call_metered_intents(since)
        for row in rows:
            intent, model, n, usd, in_tok, avg_in = row.values
            out.append({"intent": intent, "model": model, "calls": int(n), "usd": float(usd or 0.0),
                        "in_tok": None if in_tok is None else int(in_tok),
                        "avg_in": None if avg_in is None else int(avg_in)})
    except Exception:
        config.rollback_ledger_conn("calls")
    return out


def tested_recently(intent, model=None, days=14, kinds=("realtime",)):
    """True iff there's a recent SMALL test for this intent — a realtime call (a batch-1 / PROMPT-CHECK on a
    handful of items) within `days`. The signal the batch-1 gate uses to tell "you tested this prompt shape before
    scaling it to a big batch" from "first thing you did for this intent was a huge batch." Model match is optional
    (a prompt/tool bug shows on any model); pass model to require the same one. Realtime-only by default because a
    prior *batch* row carries no request-count, so it can't prove a SMALL test was run."""
    if not call_recording_consent_enabled() or not intent:
        return False
    if not kinds:                                     # empty kinds → `IN ()` is invalid SQLite → the execute would
        return False                                  # raise and get swallowed, silently returning False; say so plainly
    try:
        import datetime as _dt
        # UTC, because that is what the rows hold. A naive local now() produced '...T09:14:22' with no
        # offset and compared it as a STRING against '...T16:14:22+00:00'. West of UTC the cutoff drifts
        # into the past and stale evidence passes the test-first gate; east of it, real recent tests
        # vanish. Either way the comparison succeeds and returns a confident wrong answer.
        since = (_dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(days=int(days))).isoformat()
        from . import ledger_read
        from pathlib import Path
        if not Path(config.db_path()).exists() or not ledger_read.ledger_read_table_exists(ledger_read.LEDGER_CALLS_TABLE):
            return False
        return ledger_read.count_recent_call_tests(intent, since, kinds, model=model) > 0
    except Exception:
        return False


def cmd_summary(argv=None):
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--intent")
    a = ap.parse_args(argv)
    if not call_recording_consent_enabled():
        print("call logging is OFF — enable with `spendguard init` (calls.enabled) or SPENDGUARD_CALLS=1.")
        return 0
    rows = cost_summary(a.intent)
    if not rows:
        print("no calls recorded yet.")
        return 0
    print(f"{'intent':<20}{'model':<22}{'calls':>7}{'$cost':>11}{'good%':>7}{'$/good':>10}")
    for intent, model, n, cost, good, bad in rows:
        labeled = (good or 0) + (bad or 0)
        goodpct = f"{100*good/labeled:.0f}%" if labeled else "—"
        per = f"${cost/good:.4f}" if good else "—"
        print(f"{intent[:19]:<20}{model[:21]:<22}{n:>7}{('$%.4f' % cost):>11}{goodpct:>7}{per:>10}")
    print("\ngood% = share of LABELED calls (feedback / judge).  $/good = cost-per-good-result.")
    print("Label calls with spendguard.feedback(call_id, ok=...) or an agentic judge; reuse stays unjudged.")
    return 0

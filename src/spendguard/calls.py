"""Rich per-call context log (opt-in) — turns spend records into a cost+QUALITY corpus.

OFF by default (it can store prompts/outputs — privacy). Enable with config calls.enabled=true
(or SPENDGUARD_CALLS=1). Per call it records: chain, intent, caller, model, cost, tokens, latency,
prompt hash (+ optional snippet), output snippet, finish reason, and a DEFERRED quality label:
  - feedback(call_id, ok)        -> explicit / judge verdict (authoritative)
  - implicit 'used'              -> a later call in the same chain reused this output
That powers cost-per-GOOD-result per intent (`spendguard calls`) and, later, an `optimize` loop.

Shares the SQLite db with budget (config.db_path()), table `calls`. RLock — reentrant (record/query
hold it and call _calls_db() which re-acquires).
"""
import os, sqlite3, datetime, threading, hashlib, inspect, contextlib, uuid
from typing import Optional

from . import config

_lock = threading.RLock()
_local = threading.local()
_PKG = os.path.dirname(os.path.abspath(__file__))
_ORIGIN_SESSION = uuid.uuid4().hex[:12]
_CALL_CLASSES = frozenset(("workload", "gate_internal", "probe"))


# ── opt-in flags ──
def _truthy(v):
    return v in (True, "true", "1", 1)


def enabled():
    # PRIVACY-CRITICAL: this can store prompts/outputs, so it FAILS CLOSED. Recording is ON only for an EXPLICIT
    # affirmative env value; ANY other value — "False", "off", "disable", a typo, blank — leaves it OFF. The old
    # check was a false-word BLOCKLIST (`not in ("0","false","")`), so `SPENDGUARD_CALLS=False` was not matched and
    # silently ENABLED recording, the opposite of intent. An affirmative allowlist is the only safe direction for a
    # privacy switch: the unknown case must be "don't record", never "record". (Mirrors _truthy, case-insensitively.)
    v = os.getenv("SPENDGUARD_CALLS")
    if v is not None and v.strip() != "":
        return v.strip().lower() in ("1", "true", "yes", "on")
    return _truthy(config._cfg_get("calls", "enabled", False))


def _store_prompts(): return _truthy(config._cfg_get("calls", "store_prompts", False))
def _snip():          return int(config._cfg_get("calls", "snippet_len", 200) or 200)


# ── intent / chain context (thread-local; safe under ThreadPool) ──
def current():
    return getattr(_local, "ctx", {})


def set_context(intent: Optional[str] = None, chain: Optional[str] = None, who: Optional[str] = None,
                root_call: Optional[str] = None, attempt: Optional[int] = None,
                fell_from: Optional[str] = None, batch_expected_out: Optional[int] = None,
                call_class: Optional[str] = None, origin_session: Optional[str] = None,
                defer_batch_booking: Optional[bool] = None, requested_model: Optional[str] = None,
                redirect_reason: Optional[str] = None, resolved_lane: Optional[str] = None,
                measurement: Optional[bool] = None, gen_params: Optional[dict] = None) -> None:
    c = dict(current())
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
    _local.ctx = c                        #   (gate._record_rt fires DURING the SDK call, on this thread, so it reads ctx)


def clear_retry_context() -> None:
    """Drop the per-attempt linkage keys so a later DIRECT call on this thread never inherits a stale root_call.
    vendor_call sets them per try (propagated to the worker it runs on) and clears them when the logical call ends."""
    c = dict(current())
    c.pop("root_call", None)
    c.pop("attempt", None)
    _local.ctx = c


@contextlib.contextmanager
def fell_from_context(lane_name: str):
    """Scope `fell_from=lane_name` on the thread-local context to exactly one metered-fallback dispatch, then restore
    the prior value. The SUCCESS recorder for a metered call is gate._record_rt (the SDK-patch), which fires
    SYNCHRONOUSLY DURING the .create() call on THIS thread and reads the context via record_call — so a lane's metered
    twin, dispatched inside this block after the $0 lane went down, lands on the ledger stamped with the lane it fell
    from. Save/restore (not a bare pop) so a nested fallback never erases an outer one."""
    prev = current().get("fell_from")
    set_context(fell_from=lane_name)
    try:
        yield
    finally:
        c = dict(current())
        if prev is None:
            c.pop("fell_from", None)
        else:
            c["fell_from"] = prev
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
        con = sqlite3.connect(config.db_path())
        rows = con.execute(
            "SELECT intent, COUNT(*) c FROM calls WHERE intent IS NOT NULL AND intent != '' "
            "GROUP BY intent HAVING c >= ? ORDER BY c DESC LIMIT ?", (int(min_calls), int(limit))).fetchall()
        con.close()
        return [r[0] for r in rows]
    except Exception:
        return []


@contextlib.contextmanager
def context(intent: Optional[str] = None, chain: Optional[str] = None, contract=None):
    """`with spendguard.context(intent='loinc-typing', chain='run-42'): ...` tags the calls inside.

    On exit it emits a per-FLOW spend receipt (what ran · tokens · est→actual · running tally) — see receipt.py.
    The receipt is verbosity-gated, goes to stderr, and is fully guarded: it NEVER raises into your code.

    `contract` gives a REALTIME loop the check a batch already gets: every response is validated against the
    declared shape as it is recorded, and the flow reports how many parsed. The batch gate could refuse before
    spending; a realtime loop cannot — the money is gone call by call — so this reports EARLY and LOUDLY rather
    than gating. The failure it catches is the one that costs: call 1 parses, call 400 comes back with a
    sentence before the JSON, and the loop keeps paying. See output_contract for the accepted forms."""
    prev = getattr(_local, "ctx", None)
    set_context(intent=intent, chain=chain)
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
    if not enabled():
        return 0
    try:
        with _lock:
            r = _calls_db().execute("SELECT COALESCE(MAX(rowid),0) FROM calls").fetchone()
        return int(r[0] or 0)
    except Exception:
        return 0


def flow_agg(since_rowid: int = 0, chain: Optional[str] = None):
    """Aggregate the calls logged since `since_rowid` (a flow window) → {n, in_tok, out_tok, cost, caller}, or None
    when per-call logging is off/unavailable (the receipt then falls back to the always-on budget-$ delta)."""
    if not enabled():
        return None
    try:
        q = ("SELECT COUNT(*), COALESCE(SUM(in_tok),0), COALESCE(SUM(out_tok),0), "
             "COALESCE(SUM(cost),0.0), MAX(caller) FROM calls WHERE rowid > ?")
        a = [int(since_rowid or 0)]
        if chain:
            q += " AND chain = ?"
            a.append(chain)
        with _lock:
            row = _calls_db().execute(q, a).fetchone()
        if not row or not row[0]:
            return None
        return {"n": int(row[0]), "in_tok": int(row[1] or 0), "out_tok": int(row[2] or 0),
                "cost": float(row[3] or 0.0), "caller": row[4]}
    except Exception:
        return None


def caller():
    """Return the first real application frame, never a worker-thread trampoline."""
    try:
        for fr in inspect.stack()[2:]:
            fn = fr.filename
            base = os.path.basename(fn)
            trampoline = (base in ("thread.py", "threading.py") or
                          "concurrent/futures" in fn.replace("\\", "/"))
            if (not trampoline and not fn.startswith(_PKG) and "site-packages" not in fn
                    and fn not in ("<string>", "<stdin>")):
                return f"{os.path.basename(fn)}:{fr.function}:{fr.lineno}"
    except Exception:
        pass
    return None


# ── storage ──
def _ensure_calls_schema(c):
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
        requested_model TEXT, served_model TEXT, resolved_lane TEXT, redirect_reason TEXT)""")
    c.execute("CREATE INDEX IF NOT EXISTS idx_calls_chain ON calls(chain)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_calls_intent ON calls(intent)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_calls_ts ON calls(ts)")  # as_of/since range reads (calibrate, advise)
    # Migrate older dbs: add every column the schema gained after they were created. Column names are
    # fixed literals from this tuple (never caller input) — SQLite cannot parameterize a DDL identifier,
    # so the f-string is the only way and carries no injection surface. `executor` = which subscription
    # lane served the call; `project` = the repo it belongs to (so lane plan-value attributes like spend).
    _have = {r[1] for r in c.execute("PRAGMA table_info(calls)").fetchall()}
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
    #                      the paid twin (the mirror of `executor`, which names the lane that SERVED a $0 hit). It records
    #                      the plain FACT — the lane name — never a judgement about WHY (the reason is the row's own
    #                      outcome/provider_error and the lane_health record). `SUM(cost) WHERE fell_from='codex'` is then
    #                      the metered $ this install spent BECAUSE codex was down — the "$0 savings lost while a lane was
    #                      down" an operator restores by re-logging that lane in. NULL on a $0-lane hit or a direct call.
    for _col, _decl in (("quality_conf", "REAL"), ("executor", "TEXT"), ("project", "TEXT"),
                        ("effort", "TEXT"), ("suspect", "TEXT"),
                        ("outcome", "TEXT"), ("http_status", "INTEGER"), ("provider_error", "TEXT"),
                        ("retry_after", "REAL"), ("attempts", "INTEGER"), ("disposition", "TEXT"),
                        ("retry_of", "TEXT"), ("fell_from", "TEXT"),
                        ("call_class", "TEXT"), ("origin_session", "TEXT"),
                        ("requested_model", "TEXT"), ("served_model", "TEXT"),
                        ("resolved_lane", "TEXT"), ("redirect_reason", "TEXT")):
        if _col not in _have:
            c.execute(f"ALTER TABLE calls ADD COLUMN {_col} {_decl}")
    c.execute("CREATE INDEX IF NOT EXISTS idx_calls_executor ON calls(executor)")  # per-lane rollups
    c.execute("CREATE INDEX IF NOT EXISTS idx_calls_outcome ON calls(outcome)")    # reliability / error-class rollups
    c.execute("CREATE INDEX IF NOT EXISTS idx_calls_retry_of ON calls(retry_of)")  # reconstruct one call's retry chain
    c.execute("CREATE INDEX IF NOT EXISTS idx_calls_fell_from ON calls(fell_from)")  # per-lane fallback-spend rollups
    c.execute("CREATE INDEX IF NOT EXISTS idx_calls_class_session ON calls(call_class, origin_session)")
    c.commit()


def _calls_db():
    """The calls table, on the shared pooled ledger connection (config.pooled_ledger_conn, keyed 'calls') — reused,
    tuned, fork-safe. `_lock` still serializes the module's read-modify-write sites."""
    return config.pooled_ledger_conn("calls", _ensure_calls_schema)


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
    return (intent, chain, ((project or "").strip().lower() or None), resolved_class,
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


def record_call(provider, model, kind, cost, in_tok=0, out_tok=0, latency=None,
           prompt=None, output=None, finish=None, intent=None, chain=None, who=None,
           executor=None, project=None, effort=None, *, outcome=None, http_status=None,
           provider_error=None, retry_after=None, attempts=None, disposition=None, retry_of=None,
           fell_from=None, call_id=None, call_class=None, origin_session=None,
           requested_model=None, served_model=None, resolved_lane=None, redirect_reason=None):
    """Record one call — its full OUTCOME, success OR failure. Returns call_id. Never raises.

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

    `executor` names the SUBSCRIPTION LANE that served the call (claude-code / codex / gemini / zai-coding) when it
    rode a flat-fee plan instead of the metered API. Storing it makes "which lane worked" a recorded fact the receipt
    can show and the lane est-value stamper can price — rather than a guess inferred from the provider. `project` is
    the repo the call belongs to (derived from the live gate context when not passed), so a lane's plan VALUE
    attributes to a project exactly like billed spend does."""
    # ATTRIBUTION is resolved by the SHARED brain _attribute (enforce a paid un-intented call + resolve intent/chain/
    # project) BEFORE enabled()/try, so record_call and insert can never drift on it (the record-call-outcome DRIFT).
    intent, chain, proj, call_class, origin_session = _resolve_attribution(
        model, cost, intent, chain, project, call_class, origin_session)
    if _truthy(os.getenv("SPENDGUARD_NO_LEDGER")):
        return None                                      # explicit total opt-out (rare) — otherwise ALWAYS record
    # The OUTCOME row is recorded ALWAYS (forensic mandate); `enabled()` + store_prompts now gate ONLY the private
    # CONTENT snippets, never whether the call is on the ledger. A failure with the opt-in off MUST still land.
    _content_ok = enabled() and _store_prompts()
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
        requested_model = requested_model or ctx.get("requested_model")
        served_model = served_model or model
        resolved_lane = resolved_lane or ctx.get("resolved_lane") or executor
        redirect_reason = redirect_reason or ctx.get("redirect_reason")
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
            _calls_db().execute(
                "INSERT INTO calls (id,ts,chain,intent,caller,provider,model,kind,in_tok,out_tok,"
                "cost,latency,prompt_hash,prompt_snip,output_snip,finish,executor,project,effort,suspect,"
                "outcome,http_status,provider_error,retry_after,attempts,disposition,retry_of,fell_from,"
                "call_class,origin_session,requested_model,served_model,resolved_lane,redirect_reason) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (cid, ts, chain, intent, who or ctx.get("who") or caller(), provider, model, kind,
                 int(in_tok or 0), int(out_tok or 0), float(cost or 0), latency, ph, psnip, osnip, finish,
                 executor, proj, (effort or None), suspect,
                 (outcome or None),
                 (int(http_status) if str(http_status or "").strip().isdigit() else None),
                 (provider_error[:1000] if isinstance(provider_error, str) else None),
                 (float(retry_after) if isinstance(retry_after, (int, float)) else None),
                 (int(attempts) if isinstance(attempts, int) else None),
                 (disposition or None), (retry_of or None), (fell_from or None), call_class, origin_session,
                 (requested_model or None), (served_model or None), (resolved_lane or None),
                 (redirect_reason or None)))
            _calls_db().commit()
        # deferred implicit feedback: did THIS call reuse an earlier output in the same chain?
        if chain and prompt:
            _link_used(chain, prompt)
        return cid
    except Exception:
        config.rollback_ledger_conn("calls")   # a failed write left a txn open on the REUSED pooled conn holding the
        return None                            # shared WAL write lock — clear it so it can't stall other writers



_CONF = {"explicit": 1.0, "judge": 0.95, "used": 0.6, "mined": 0.5}


def feedback(call_id: Optional[str], ok: bool = True, source: str = "explicit",
             confidence: Optional[float] = None) -> None:
    """Label a call's quality after the fact (judge verdict, human accept, downstream validation).
    Carries a confidence (explicit=1.0, judge=0.95, used=0.6, mined=0.5) the advisor weights by."""
    if not call_id:
        return
    conf = confidence if confidence is not None else _CONF.get(source, 0.7)
    try:
        with _lock:
            _calls_db().execute("UPDATE calls SET quality=?, quality_src=?, quality_conf=? WHERE id=?",
                          ("good" if ok else "bad", source, conf, call_id))
            _calls_db().commit()
    except Exception:
        config.rollback_ledger_conn("calls")   # clear a dangling txn from the failed write on the reused pooled conn


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
    with _lock:
        _calls_db().execute(
            "INSERT INTO calls (id,ts,chain,intent,caller,provider,model,kind,in_tok,out_tok,"
            "cost,quality,quality_src,quality_conf,effort,project,call_class,origin_session) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (cid, ts, chain, intent, who, provider, model, kind,
             int(in_tok or 0), int(out_tok or 0), float(cost or 0), quality, quality_src, quality_conf,
             (effort or None), proj, call_class, origin_session))
        _calls_db().commit()
    return cid


def reconcile_calls_batch_estimate(provider, model, intent, in_tok, out_tok, cost):
    """Revise the newest matching provisional call row to provider-measured batch usage, preserving the row."""
    with _lock:
        con = _calls_db()
        row = con.execute(
            "SELECT id FROM calls WHERE kind='batch' AND provider=? AND model=? AND intent IS ? "
            "ORDER BY rowid DESC LIMIT 1", (provider, model, intent)).fetchone()
        if not row:
            return False
        con.execute("UPDATE calls SET in_tok=?, out_tok=?, cost=? WHERE id=?",
                    (int(in_tok or 0), int(out_tok or 0), float(cost or 0), row[0]))
        con.commit()
    return True


def _link_used(chain, current_prompt):
    """Mark prior unlabeled calls in this chain 'used' if their output appears in this prompt."""
    if not _store_prompts():
        return
    try:
        with _lock:
            rows = _calls_db().execute(
                "SELECT id, output_snip FROM calls WHERE chain=? AND quality IS NULL "
                "AND output_snip IS NOT NULL ORDER BY ts DESC LIMIT 10", (chain,)).fetchall()
            for cid, out in rows:
                # Match the FULL stored snippet (not just an 80-char prefix) and require it to be substantive (>= 40
                # chars): an 80-char PREFIX match fired on any two calls that shared a boilerplate/system preamble,
                # mislabelling unrelated outputs 'used'. Requiring the whole snippet to reappear, and skipping short
                # boilerplate, keeps the signal specific. (Still a low-confidence 0.6 'used' label, never authoritative.)
                if out and len(out) >= 40 and out in current_prompt:
                    _calls_db().execute("UPDATE calls SET quality='good', quality_src='used', quality_conf=0.6 WHERE id=?", (cid,))
            _calls_db().commit()
    except Exception:
        config.rollback_ledger_conn("calls")   # clear a dangling txn from the failed write on the reused pooled conn


def intent_spend(intent, window_s=86400):
    """The ACTUAL metered $ recorded for `intent` in the last `window_s` seconds — what guardrail D's per-intent
    running cap (enforced at the call door, gate._rt_precheck_usd) sums to decide whether the next call would cross the
    ceiling. Rolling window; ts is ISO-8601 UTC so a lexical `>=` is chronological. Returns 0.0 on empty / error (a
    cap can only ADD safety, never break a call because the ledger hiccuped). This is the path-independent twin of
    bulk_delegate's in-fan budget_usd: every metered call reaches the door, however it was issued."""
    if not intent:
        return 0.0
    import datetime
    cut = (datetime.datetime.now(datetime.timezone.utc)
           - datetime.timedelta(seconds=max(1, int(window_s)))).isoformat(timespec="seconds")
    try:
        with _lock:
            r = _calls_db().execute("SELECT COALESCE(SUM(cost),0) FROM calls WHERE intent=? AND ts>=?",
                                    (intent, cut)).fetchone()
        return float(r[0] or 0.0) if r else 0.0
    except Exception:
        config.rollback_ledger_conn("calls")
        return 0.0


def cost_summary(intent=None):
    """Per (intent, model): calls, $ total, %good, and cost-per-good-result."""
    cond = ["(intent IS NULL OR intent NOT LIKE 'spendguard:%')"]   # exclude spendguard's own meta calls
    args = []
    if intent:
        cond.append("intent=?"); args.append(intent)
    # SQLi-safe (scanner false positive): `cond` holds only STATIC predicate strings ("intent=?"); every VALUE is bound
    # via `args` as a `?` parameter. The f-string below interpolates this fixed WHERE clause, never user data.
    where, args = ("WHERE " + " AND ".join(cond), tuple(args))
    with _lock:
        rows = _calls_db().execute(
            f"""SELECT COALESCE(intent,'(none)'), COALESCE(model,'?'), COUNT(*), COALESCE(SUM(cost),0),
                   SUM(CASE WHEN quality='good' THEN 1 ELSE 0 END),
                   SUM(CASE WHEN quality='bad'  THEN 1 ELSE 0 END)
                FROM calls {where} GROUP BY intent, model ORDER BY SUM(cost) DESC""", args).fetchall()
    return rows


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
        with _lock:
            predicates = ["intent=?", "out_tok IS NOT NULL", "out_tok > 0", "suspect IS NULL"]
            args = [intent]
            if call_class is not None:
                predicates.append("call_class=?")
                args.append(call_class)
            if origin_session is not None:
                predicates.append("origin_session=?")
                args.append(origin_session)
            rows = _calls_db().execute(
                "SELECT COALESCE(NULLIF(executor,''), provider), COALESCE(model,'?'), "
                "COALESCE(AVG(out_tok), 0), COUNT(*) FROM calls "
                f"WHERE {' AND '.join(predicates)} "
                "GROUP BY COALESCE(NULLIF(executor,''), provider), model", args).fetchall()
        for ex, model, avg_out, n in rows:
            out[(ex or "?", model or "?")] = {"mean_out": float(avg_out or 0.0), "n": int(n)}
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
        predicates = ["COALESCE(NULLIF(executor,''), provider)=?"]
        args = [executor]
        if since is not None:
            predicates.append("ts>=?"); args.append(since)
        if call_class is not None:
            predicates.append("call_class=?"); args.append(call_class)
        with _lock:
            rows = _calls_db().execute(
                "SELECT COALESCE(model,'?'), COALESCE(outcome,''), COUNT(*), MAX(ts) FROM calls "
                f"WHERE {' AND '.join(predicates)} "
                "GROUP BY COALESCE(model,'?'), COALESCE(outcome,'')", args).fetchall()
        for model, outcome, n, last_ts in rows:
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
        with _lock:
            rows = _calls_db().execute(
                "SELECT COALESCE(intent,'(none)'), COALESCE(model,'?'), COUNT(*), COALESCE(SUM(cost),0), "
                "COALESCE(SUM(in_tok),0), COALESCE(AVG(in_tok),0) FROM calls "
                "WHERE cost > 0 AND ts >= ? AND COALESCE(NULLIF(executor,''),'')='' AND kind='realtime' "
                "AND (intent IS NULL OR intent NOT LIKE 'spendguard:%') "
                "GROUP BY intent, model ORDER BY SUM(cost) DESC", (since,)).fetchall()
        for intent, model, n, usd, in_tok, avg_in in rows:
            out.append({"intent": intent, "model": model, "calls": int(n), "usd": float(usd or 0.0),
                        "in_tok": int(in_tok or 0), "avg_in": int(avg_in or 0)})
    except Exception:
        config.rollback_ledger_conn("calls")
    return out


def tested_recently(intent, model=None, days=14, kinds=("realtime",)):
    """True iff there's a recent SMALL test for this intent — a realtime call (a batch-1 / PROMPT-CHECK on a
    handful of items) within `days`. The signal the batch-1 gate uses to tell "you tested this prompt shape before
    scaling it to a big batch" from "first thing you did for this intent was a huge batch." Model match is optional
    (a prompt/tool bug shows on any model); pass model to require the same one. Realtime-only by default because a
    prior *batch* row carries no request-count, so it can't prove a SMALL test was run."""
    if not enabled() or not intent:
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
        # SQLi-safe (scanner false positive): `%s` is filled with the right COUNT of `?` placeholders, NOT values;
        # the kind values go through `args`. (Parameterized — no user data is concatenated into the SQL string.)
        q = "SELECT COUNT(*) FROM calls WHERE intent=? AND ts>=? AND kind IN (%s)" % ",".join("?" * len(kinds))
        args = [intent, since, *kinds]
        if model:
            q += " AND model=?"; args.append(model)
        with _lock:
            return _calls_db().execute(q, args).fetchone()[0] > 0
    except Exception:
        return False


def cmd_summary(argv=None):
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--intent")
    a = ap.parse_args(argv)
    if not enabled():
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
    print("\ngood% = share of LABELED calls (feedback / judge / implicit 'used').  $/good = cost-per-good-result.")
    print("Label calls with spendguard.feedback(call_id, ok=...) or let chains infer 'used'.")
    return 0

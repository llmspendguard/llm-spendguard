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
import os, sqlite3, datetime, threading, hashlib, inspect, contextlib
from typing import Optional

from . import config

_lock = threading.RLock()
_local = threading.local()
_PKG = os.path.dirname(os.path.abspath(__file__))


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
                fell_from: Optional[str] = None) -> None:
    c = dict(current())
    if intent is not None:
        c["intent"] = intent
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
    try:
        for fr in inspect.stack()[2:]:
            fn = fr.filename
            if not fn.startswith(_PKG) and "site-packages" not in fn and fn not in ("<string>", "<stdin>"):
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
        executor TEXT, project TEXT, effort TEXT, suspect TEXT)""")
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
                        ("retry_of", "TEXT"), ("fell_from", "TEXT")):
        if _col not in _have:
            c.execute(f"ALTER TABLE calls ADD COLUMN {_col} {_decl}")
    c.execute("CREATE INDEX IF NOT EXISTS idx_calls_executor ON calls(executor)")  # per-lane rollups
    c.execute("CREATE INDEX IF NOT EXISTS idx_calls_outcome ON calls(outcome)")    # reliability / error-class rollups
    c.execute("CREATE INDEX IF NOT EXISTS idx_calls_retry_of ON calls(retry_of)")  # reconstruct one call's retry chain
    c.execute("CREATE INDEX IF NOT EXISTS idx_calls_fell_from ON calls(fell_from)")  # per-lane fallback-spend rollups
    c.commit()


def _calls_db():
    """The calls table, on the shared pooled ledger connection (config.pooled_ledger_conn, keyed 'calls') — reused,
    tuned, fork-safe. `_lock` still serializes the module's read-modify-write sites."""
    return config.pooled_ledger_conn("calls", _ensure_calls_schema)


def _uuid():
    import uuid
    return uuid.uuid4().hex[:16]


def _resolve_attribution(model, cost, intent, chain, project):
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
    return intent, chain, ((project or "").strip().lower() or None)   # lowercased project_primary for clean joins


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
           fell_from=None, call_id=None):
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
    intent, chain, proj = _resolve_attribution(model, cost, intent, chain, project)
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
                "outcome,http_status,provider_error,retry_after,attempts,disposition,retry_of,fell_from) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (cid, ts, chain, intent, who or ctx.get("who") or caller(), provider, model, kind,
                 int(in_tok or 0), int(out_tok or 0), float(cost or 0), latency, ph, psnip, osnip, finish,
                 executor, proj, (effort or None), suspect,
                 (outcome or None),
                 (int(http_status) if str(http_status or "").strip().isdigit() else None),
                 (provider_error[:1000] if isinstance(provider_error, str) else None),
                 (float(retry_after) if isinstance(retry_after, (int, float)) else None),
                 (int(attempts) if isinstance(attempts, int) else None),
                 (disposition or None), (retry_of or None), (fell_from or None)))
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
           quality=None, quality_src=None, quality_conf=None, who="backfill", effort=None, project=None):
    """Low-level insert used by backfill (ungated) and the bakeoff/titration (per-EFFORT arms, with an inline quality
    label). Returns call_id. Shares the ATTRIBUTION brain (_attribute) with record_call — so a paid row here enforces
    intent + records the project exactly like a live call, never a second un-attributed INSERT (the
    record-call-outcome DRIFT). `effort` = the reasoning tier this row was produced at (sliceable evidence)."""
    intent, chain, proj = _resolve_attribution(model, cost, intent, chain, project)
    cid = _uuid()
    ts = ts or datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")
    with _lock:
        _calls_db().execute(
            "INSERT INTO calls (id,ts,chain,intent,caller,provider,model,kind,in_tok,out_tok,"
            "cost,quality,quality_src,quality_conf,effort,project) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (cid, ts, chain, intent, who, provider, model, kind,
             int(in_tok or 0), int(out_tok or 0), float(cost or 0), quality, quality_src, quality_conf,
             (effort or None), proj))
        _calls_db().commit()
    return cid


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

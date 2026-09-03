"""Cross-process spend ledger (SQLite, WAL) for fleet-wide DAILY / MONTHLY caps — no proxy.

Enabled by config `budget.backend = sqlite`. The gate records every charge here and checks cumulative
spend across ALL processes before allowing more. Default `backend = memory` keeps the per-process
real-time cap only (this module is then never touched). Per-call SQLite I/O is fine for moderate
real-time volume; very high-volume loops should stay on the in-process cap.
"""
import contextlib, sqlite3, datetime, threading
from . import config

_conn = None
_lock = threading.RLock()   # reentrant: record_charge()/spent_since() hold it AND call _ledger_db() which re-acquires


def _ledger_db():
    """A WAL SQLite connection to the ledger file, shared across this module and by guard.py's `savings` table.
    It NO LONGER creates the retired `charges` table: the money-of-record is `spend_events` (owned by
    SpendLedger), every reader/writer here goes through it, and a fresh install has no `charges` to make.
    The one-time `migrate_charges` bridge reads a PRE-EXISTING charges via its own connection; it never needs
    this to create one, and the migration tests build their own charges fixture."""
    global _conn
    if _conn is None:
        with _lock:
            if _conn is None:
                c = sqlite3.connect(config.db_path(), timeout=10, check_same_thread=False)
                c.execute("PRAGMA journal_mode=WAL")
                c.commit()
                _conn = c
    return _conn


_PROJECT = None


# The ONE definition of "money spent in a period" now lives on the money-of-record as SpendLedger._COUNTABLE
# (exclude meta / reconciliation markers / quarantined-impossible / reconstructed; an estimate still binds a
# cap). It replaced the countable_charges VIEW over the retired `charges` table — same rule, one place.


_LEDGER_TL = threading.local()


def _ledger():
    """A SpendLedger over this database, with a THREAD-LOCAL sqlite connection.

    WHY THREAD-LOCAL, NOT ONE SHARED CONNECTION. A single cached connection DEADLOCKED under the concurrent
    fan_out panel and hung a run for 8 minutes. sqlite serializes every operation on a connection behind one
    mutex, and our `dec_sum` custom aggregate calls back into Python — so a thread mid-SUM held the connection
    mutex and wanted the GIL, while another thread held the GIL and wanted the mutex. The SIGABRT stack proved
    it: step_callback/take_gil on one thread, vdbeUnbind/pthread_mutex_lock on another. Per-thread connections
    never share a mutex, so that read-vs-write deadlock is structurally impossible. Writes stay serialized for
    the hash-chained audit log by `_lock` in the record path; WAL makes each committed write visible to the
    other threads' connections at once."""
    led = getattr(_LEDGER_TL, "led", None)
    if led is None:
        from .ledger import SpendLedger
        led = _LEDGER_TL.led = SpendLedger()
    return led


def _reset_ledger():
    """Drop THIS thread's cached ledger connection so the next read reconnects clean — call after a destructive
    schema change. Replaces the old `budget._LEDGER = None`, which a thread-local cache cannot honor."""
    _LEDGER_TL.led = None


def _reset_after_fork():
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
    _os.register_at_fork(after_in_child=_reset_after_fork)
except (AttributeError, ValueError, ImportError):   # register_at_fork is POSIX-only — elsewhere this is a no-op
    pass


def _attribute(ev, project):
    """Stamp org/team/project on a spend_event from the charge's project tag via the prior repo→org map — a
    cheap cached dict lookup, NOT an LLM (agentic refinement is the later attribute/reconcile pass, never the
    hot path). Keeps the same attribution the charges ledger carried, in the typed columns."""
    proj = (project or "").strip().lower()
    try:
        from . import conv as _conv_mod
        org, team = _conv_mod._prior_org_team(proj) if proj else ("", "")
    except Exception:
        org, team = "", ""
    ev["project_primary"] = proj
    ev["projects"] = [proj] if proj else []
    ev["org"], ev["team"] = org, team


def _record_spend_event(provider, model, kind, cost, *, conv_id="", basis="", intent="", actor="", key_fp="",
                        project="", occurred_at=None, in_tok=0, out_tok=0, cache_read_tok=0, cache_write_tok=0,
                        reasoning_tok=0, source="gate", dedup_suffix="", invoice_id="", dedup_key=None):
    """THE write for a live charge → `spend_events`, the single money-of-record, through the ONE shared mapping
    (charge_to_event). Every budget writer records through here; there is no second ledger, and — since the
    cutover — no `charges` fallback behind it, so a dropped write is a dropped charge. The ledger connection
    carries sqlite's own busy_timeout (see SpendLedger, `timeout=`), so an ordinary transient lock blocks briefly
    and clears rather than failing. Fail-OPEN only as a last resort: warn LOUDLY and let the caller proceed
    (losing the user's work over a bookkeeping hiccup is worse than a missed row), and even then the miss is
    recoverable from provider truth via `spendguard reconcile`."""
    try:
        from .ledger import live_dedup_key
        ev = charge_to_event(provider, model, kind, cost, conv_id=conv_id, basis=basis,
                             intent=intent, actor=actor, key_fp=key_fp)
        _attribute(ev, project)
        oa = occurred_at or datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")
        ev["occurred_at"] = ev["ts_utc"] = oa
        ev["in_tok"], ev["out_tok"] = int(in_tok or 0), int(out_tok or 0)
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
        with _lock:
            _ledger().record_event(ev)
    except Exception as e:
        # spend_events is the SOLE ledger now, so a failed write means this charge is genuinely absent — NOT
        # sitting safe in `charges` (dropped) and NOT rebuildable by `spendguard migrate` (which read charges).
        # DURABLY CAPTURE IT so the loss never depends on this stderr being seen: a headless/daemon consumer
        # discards stderr, and provider-truth reconcile recovers BATCH spend but cannot re-book a realtime charge
        # without an admin key. The dead-letter file is the honest record of exactly what the ledger dropped.
        try:
            import json as _json
            with open(config.HOME / "spend_events_deadletter.jsonl", "a") as _dl:
                _dl.write(_json.dumps({
                    "provider": provider, "model": model, "kind": kind, "cost": str(cost),
                    "in_tok": int(in_tok or 0), "out_tok": int(out_tok or 0), "occurred_at": occurred_at,
                    "intent": intent, "project": project, "source": source,
                    "error": f"{type(e).__name__}: {str(e)[:120]}"}) + "\n")
        except Exception:
            pass
        import sys as _sys
        _sys.stderr.write(
            f"[budget] WARN spend_events write failed for {provider}/{model} ${cost} "
            f"({type(e).__name__}: {str(e)[:100]}) — captured to spend_events_deadletter.jsonl (this charge is "
            f"MISSING from the ledger; spend_events is the sole money-of-record). `spendguard reconcile` re-books "
            f"batch spend from provider truth.\n")


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
           intent=None, actor=None):
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
    now = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")
    _record_spend_event(provider, model, kind, float(cost), conv_id=conv or "", basis=basis or "",
                        intent=(intent or "")[:120], actor=(actor or "")[:120], key_fp=fp, project=proj or "",
                        occurred_at=now, source="gate")


def snapshot(reason="", keep=20):
    """Copy the ledger database aside BEFORE anything mutates it. Returns the path, or None.

    WHY THIS EXISTS. On 2026-08-10 a guard test overwrote ~/.spendguard/config.json — 9KB of settings
    replaced by a 26-byte probe — and there was no backup to restore from: the machine's off-site job
    mirrors ~/.claude only, and this directory holds the ledger itself (43,488 rows, $24,515 recorded at
    the time). Every re-attribution and quarantine that day ran against data with no recovery path.

    A mutation without a recovery path is not a change, it is a gamble. sqlite's own backup API is used so
    a snapshot taken while another process is mid-write is still consistent — a torn copy would be a backup
    that only fails when you need it."""
    import datetime as _dt
    import sqlite3 as _sq
    try:
        src = config.db_path()
        d = config.HOME / "snapshots"
        d.mkdir(parents=True, exist_ok=True)
        stamp = _dt.datetime.now(_dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        tag = "".join(ch if ch.isalnum() or ch in "-_" else "-" for ch in (reason or "pre-mutation"))[:40]
        dst = d / f"spend-{stamp}-{tag}.db"
        with _sq.connect(src) as _s, _sq.connect(str(dst)) as _t:
            _s.backup(_t)                        # consistent even under a concurrent writer
        # Keep the most recent `keep`; a snapshot directory that grows without bound gets deleted by hand
        # one day, which is the same as having none.
        old = sorted(d.glob("spend-*.db"))[:-keep] if keep else []
        for f in old:
            try:
                f.unlink()
            except OSError:
                pass
        return str(dst)
    except Exception as e:
        import sys as _sys
        _sys.stderr.write(f"[budget] WARN could not snapshot the ledger before mutating it "
                          f"({type(e).__name__}: {str(e)[:80]}) — the change is NOT protected.\n")
        return None


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


def projects_for_conv(conv):
    """Distinct repos (projects) THIS conversation touched — its workload spend. Powers the contextual receipt's
    collapsed view (a conversation can span repos; this very chat touched llm-spendguard + manga2anime + lmm)."""
    if not conv:
        return []
    return _ledger().distinct("project_primary", where={"conv_id": str(conv), "is_meta": 0})


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
        p = snapshot(reason=reason)
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


def charge_to_event(provider, model, kind, cost, conv_id="", basis="", intent="", actor="", key_fp=""):
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
        "intent": (intent or "")[:120], "actor": (actor or "")[:120],
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


def spent_since(day, project=None, conv=None):
    """Gate-recorded workload $ since `day` — the LLM cap's number, now read from spend_events via the single
    `_COUNTABLE` filter (batch+realtime, excluding meta / reconciliation / voided-impossible / reconstructed).
    Optionally SCOPE to a `project` (repo) and/or `conv` (conversation) — the receipt uses this to show what is
    relevant to the current repo/conversation, not a global sum.

    Repointed onto spend_events (the money-of-record). This is LLM-only by construction: GPU/remote spend has
    its OWN cap (resources.compute_exceeded) and is no longer lumped into the LLM total as the old
    countable_charges view did (charges carried no remote rows, so the live number is unchanged)."""
    where = {}
    if project is not None:
        where["project_primary"] = str(project).strip().lower()
    if conv is not None:
        where["conv_id"] = str(conv)
    return float(_ledger().spent_dec(since=day, where=where or None))


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


def by_basis(day):
    """{basis: {'cost': $, 'n': rows}} since `day` — how much of the headline is a projection vs a bill, read
    from cost_basis on spend_events. Excludes meta + reconciliation mirrors + quarantine (void) + unpriced.
    Unlabelled legacy rows come back under '' and are shown as unknown, never folded into 'billed'."""
    led = _ledger()
    res = led.sum_by("cost_basis", since=day, where={"is_meta": 0, "reconciled": 0}, filt=led._NOT_UNPRICED)
    return {(b or ""): {"cost": float(v["usd"]), "n": v["n"]} for b, v in res.items()}


# ── reconciliation: make the LOCAL ledger reflect PROVIDER-billed truth (the gap = ungoverned/pre-ledger spend) ──
def by_provider_day(kind=None, since=None):
    """{(provider, day): $} of GATE-recorded COUNTABLE spend — the attributed side of reconcile. Read from
    spend_events via the single _COUNTABLE filter (reconciliation mirrors, meta, quarantine, reconstructed all
    excluded — like-for-like vs provider truth). `kind` restricts to that category's money column."""
    led = _ledger()
    cols = [led.category_col(kind)] if kind else None
    res = led.sum_by(["provider", "day"], cols=cols, filt=led._COUNTABLE, since=since)
    return {(p or "?", d): float(v["usd"]) for (p, d), v in res.items() if float(v["usd"]) != 0}


def reconciled_by_project(since=None):
    """{project: $} of RECONCILED batch-marker rows (recon_marker='(provider-batch)' — the provider-truth gap
    reconcile_into_ledger attributed by evidence). The 'attributed' side of the reconcile loop, complement to
    gate_by_project_day."""
    res = _ledger().sum_by("project_primary", since=since, where={"recon_marker": _RECONCILED})
    return {(p or "unattributed"): float(v["usd"]) for p, v in res.items()}


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


def clear_reconciled(since=None, model=None):
    """Remove prior reconciliation rows so reconcile is idempotent (rebuilds them). Keyed by the marker model
    (default the batch marker; the realtime backfill passes its own)."""
    snapshot_once("clear-reconciled")           # DELETEs money rows — never without a recovery path
    marker = model or _RECONCILED
    # Delete the reconciliation-mirror rows from spend_events (the money-of-record). NOT charges: its writer
    # record_reconciled writes only spend_events (the INSERT INTO charges was removed 6 lines above in this
    # same change), reconciled_by_project reads spend_events (repointed 5b, commit 6969746), and `grep
    # 'FROM charges'` across src is EMPTY. So charges holds no reconciliation rows to leave stale.
    _ledger().delete(where={"recon_marker": marker}, since=since, actor="clear_reconciled", reason="rebuild")


# ── estimate→actual true-down (ledger_sync.true_down writes these; the gate's batch rows are PRE-SUBMIT
#    estimates, so once the provider bills the actuals the ledger must come down to the billed truth) ──
# RAW-CHARGES-OK: excludes true-down as well, which the view deliberately keeps
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


def record_true_down(day, provider, model, delta, project):
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
                        invoice_id="%s:%s" % (provider, (day or "")[:7]))   # reconciled to this provider-period invoice


def clear_true_down(since=None):
    """Remove prior true-down rows in the window so the correction is rebuilt idempotently each reconcile
    (billed actuals only grow as in-flight batches land, so each rebuild converges on the final billed $).

    CLEARS EVERY PROVIDER ON PURPOSE, including one whose billed fetch just failed. A review finding said
    this was a bug — clearing corrections for a provider we could not read — and test_true_down.py, which
    predates it, says otherwise and explains why: a failed fetch means that provider is NEVER trued down,
    so its rows fall back to the gate ESTIMATE, which is labelled `basis=estimate` and reads as one.
    Keeping the previous correction would leave a figure LABELLED as billed truth while being derived from
    billed data this run could not confirm — a stale number wearing a stronger label. The estimate is the
    more honest of the two, so the clear stays unconditional and ledger_sync warns about the gap instead.
    Second finding today that both validators confirmed and the code was right about."""
    snapshot_once("clear-true-down")    # DELETEs money rows — never without a recovery path
    # Delete the true-down correction rows from spend_events (the money-of-record). NOT charges: the ONLY writer
    # of true-down rows, record_true_down, now writes only spend_events (its INSERT INTO charges was removed
    # above); and the only consumers of these rows — gate_batch_cells (the estimate base) and by_dims (the SaaS
    # push) — read spend_events (repointed 5b). So charges holds no true-down rows for this to leave stale.
    _ledger().delete(where={"conv_id": _TRUE_DOWN_CONV}, since=since, actor="clear_true_down", reason="rebuild")


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


def external_spent_since(day):
    """Real EXTERNAL (MCP/tool + external API) $ recorded since `day`, from the ledger's external axis — its OWN
    number, never mixed into the LLM cap (spent_since reads only batch+realtime)."""
    return round(sum(by_day(kind="external", since=day).values()), 6)


def external_spent_today():
    return external_spent_since(_utc().strftime("%Y-%m-%d"))


def external_exceeded(pending=0.0):
    """(‑, cap, projected) if today's external spend + `pending` would breach the external cap, else None. ONE read
    of the spent total so the value compared and the value reported can't diverge (same discipline as meta_exceeded)."""
    cap = config.external_cap()
    if cap is None:
        return None                                     # no external cap set → gating is a no-op (opt-in)
    spent = external_spent_today() + float(pending or 0)
    return ("external", cap, spent) if spent > cap else None


def meta_spent_since(day):
    """spendguard's OWN (meta) spend since `day` — the is_meta rows, on their own cap line. Read from
    spend_events via meta_dec (excludes voided/reversed; a meta row is never quarantine/unpriced)."""
    return float(_ledger().meta_dec(since=day))


def meta_spent_today():
    return meta_spent_since(_utc().strftime("%Y-%m-%d"))


def meta_exceeded(pending=0.0):
    cap = config.meta_cap()
    # ONE READ. Called twice, the value compared against the cap and the value REPORTED to the user were
    # two different measurements with a concurrent write possible between them — so the message could name
    # a total that does not breach the cap it says was breached, which reads as a bug in the cap itself.
    spent = meta_spent_today() + pending
    if cap is not None and spent > cap:
        return ("meta", cap, spent)
    return None


# RAW-CHARGES-OK: carries its own kind/meta switch per caller; markers already excluded
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
                     cols=BILLED_USD_COLS, since=since, filt=led._NOT_UNPRICED + " AND COALESCE(billed, 1) = 1")
    out = []
    for (day, prov, model, ctype, ismeta, proj), v in res.items():
        kind = "meta" if ismeta else (ctype or "workload")
        out.append(dict(day=day, provider=prov or "?", model=model or "?", kind=kind,
                        project=proj or "", cost=float(v["usd"]), calls=v["n"]))
    return out


# RAW-CHARGES-OK: excludes true-down as well, which the view deliberately keeps
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


def exceeded(pending=0.0, kind="llm"):
    """(scope, cap, projected) if a cap would be exceeded by `pending` more $ on a call of resource class
    `kind` (llm|compute), else None. Checks the class SUB-CAP then the TOTAL ceiling, daily then monthly.
    The gate governs LLM calls, so it passes kind='llm'; remote-compute caps are checked in resources.py
    (vast.ai launches don't hit the gate). meta is separate (meta_exceeded). NOTE: this local ledger holds LLM
    spend, so the total ceiling here is evaluated against LLM spend — the true LLM+compute total is composed on
    the dashboard/report; the compute portion is enforced/alerted via resources.compute_exceeded()."""
    sd, sm = spent_today(), spent_month()
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
        if capv is not None and sp + pending > capv:
            return (scope, capv, sp + pending)
    return None

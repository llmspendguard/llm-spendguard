"""Cross-process spend ledger (SQLite, WAL) for fleet-wide DAILY / MONTHLY caps — no proxy.

Enabled by config `budget.backend = sqlite`. The gate records every charge here and checks cumulative
spend across ALL processes before allowing more. Default `backend = memory` keeps the per-process
real-time cap only (this module is then never touched). Per-call SQLite I/O is fine for moderate
real-time volume; very high-volume loops should stay on the in-process cap.
"""
import contextlib, sqlite3, datetime, threading
from . import config

_conn = None
_lock = threading.RLock()   # reentrant: record()/spent_since() hold it AND call _db() which re-acquires


def _db():
    global _conn
    if _conn is None:
        with _lock:
            if _conn is None:
                c = sqlite3.connect(config.db_path(), timeout=10, check_same_thread=False)
                c.execute("PRAGMA journal_mode=WAL")
                c.execute("CREATE TABLE IF NOT EXISTS charges "
                          "(ts TEXT, day TEXT, provider TEXT, model TEXT, kind TEXT, cost REAL, "
                          "project TEXT DEFAULT '', conv_id TEXT DEFAULT '')")
                cols = [r[1] for r in c.execute("PRAGMA table_info(charges)").fetchall()]
                if "project" not in cols:                      # migrate older ledgers
                    c.execute("ALTER TABLE charges ADD COLUMN project TEXT DEFAULT ''")
                if "conv_id" not in cols:                      # conversation/chat id per call (links to the chat)
                    c.execute("ALTER TABLE charges ADD COLUMN conv_id TEXT DEFAULT ''")
                if "key_fp" not in cols:                       # which provider key served the call (sha8:last4, local-only)
                    c.execute("ALTER TABLE charges ADD COLUMN key_fp TEXT DEFAULT ''")
                if "basis" not in cols:                        # WHAT KIND of number this is (see BASES)
                    c.execute("ALTER TABLE charges ADD COLUMN basis TEXT DEFAULT ''")
                # WHO DID WHAT. A ledger that cannot say what a dollar BOUGHT and WHAT RAN IT is a total,
                # not an account. Until these existed the money table held provider/model/cost/project and
                # the purpose lived only in `calls` — a separate table with no join key back to the charge —
                # so "what was this $23 for?" was unanswerable from the authoritative record.
                if "intent" not in cols:                       # WHAT the spend was for (calls.context)
                    c.execute("ALTER TABLE charges ADD COLUMN intent TEXT DEFAULT ''")
                if "actor" not in cols:                        # WHAT RAN IT (entrypoint:function:line)
                    c.execute("ALTER TABLE charges ADD COLUMN actor TEXT DEFAULT ''")
                c.execute("CREATE INDEX IF NOT EXISTS idx_day ON charges(day)")
                c.execute("CREATE INDEX IF NOT EXISTS idx_charges_conv ON charges(conv_id)")  # chat↔charge joins (attribution)
                c.execute("CREATE INDEX IF NOT EXISTS idx_charges_keyfp ON charges(key_fp)")  # per-key spend view
                _create_countable_view(c)
                c.commit()
                _conn = c
    return _conn


_PROJECT = None


COUNTABLE_VIEW = "countable_charges"


def _create_countable_view(c):
    """ONE definition of "money spent in a period", as a SQL view every reader can use.

    THE REASON THIS EXISTS. `charges` holds rows that mean different things — real charges, pre-spend
    estimates, provider-batch reconciliation, realtime backfills, quarantined impossibilities — and until now
    EVERY reader rebuilt its own WHERE clause to exclude the ones that are not period spend. Twelve of them in
    this module alone. Each one had to remember the full marker set, and they did not: in a single day a
    $359.63 phantom from reading batch history blocked the daily cap, a $10,409.24 backfill dated today
    blocked the monthly cap, relabelling that row's basis was undone by its writer, and `_MARKER_MODELS` sat
    defined-and-unreferenced the whole time. Four incidents, one cause: no single answer to "what counts".

    Rebuilt on every connect, so it always reflects the CURRENT marker set rather than whatever was true when
    some database file was first created.

    Deliberately NOT filtered here: `basis`. An estimate must still bind a pre-spend cap — that is the whole
    point of estimating — so the view carries it and callers that want billed-only filter further."""
    c.execute(f"DROP VIEW IF EXISTS {COUNTABLE_VIEW}")
    marks = ",".join("'" + m.replace("'", "''") + "'" for m in _MARKER_MODELS)
    c.execute(
        f"CREATE VIEW {COUNTABLE_VIEW} AS SELECT * FROM charges WHERE "
        f"(kind IS NULL OR kind != 'meta') "                       # meta is spendguard's own overhead, tracked apart
        f"AND (model IS NULL OR model NOT IN ({marks})) "          # synthetic marker rows: reconciliation, backfills
        f"AND (conv_id IS NULL OR conv_id != '{QUARANTINE_CONV}') "  # impossible estimates: never money
        f"AND (basis IS NULL OR basis != '{BASIS_RECONSTRUCTED}')")  # a restatement of history, already counted


_LEDGER = None


def _ledger():
    """The SpendLedger over this same database — the one writer for the hash-chained audit log."""
    global _LEDGER
    if _LEDGER is None:
        from .ledger import SpendLedger
        _LEDGER = SpendLedger()
    return _LEDGER


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


def _shadow_spend_event(provider, model, kind, cost, *, conv_id="", basis="", intent="", actor="", key_fp="",
                        project="", occurred_at=None, in_tok=0, out_tok=0, source="gate", dedup_suffix=""):
    """Write the same charge into `spend_events` — the money-of-record being cut over to — through the ONE
    shared mapping (charge_to_event). Fail-OPEN during the transition: a problem here must never crash the
    caller's flow (the `charges` write is still authoritative), but it warns LOUDLY so a silent drop is
    impossible. This shadow write is removed in the final cutover stage, when the charges writes stop."""
    try:
        from .ledger import live_dedup_key
        ev = charge_to_event(provider, model, kind, cost, conv_id=conv_id, basis=basis,
                             intent=intent, actor=actor, key_fp=key_fp)
        _attribute(ev, project)
        oa = occurred_at or datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")
        ev["occurred_at"] = ev["ts_utc"] = oa
        ev["in_tok"], ev["out_tok"] = int(in_tok or 0), int(out_tok or 0)
        ev["dedup_key"] = live_dedup_key(oa + dedup_suffix)
        ev["source"] = ev["recorded_by"] = source
        with _lock:
            _ledger().record(ev)
    except Exception as e:
        import sys as _sys
        _sys.stderr.write(f"[budget] WARN shadow spend_events write failed "
                          f"({type(e).__name__}: {str(e)[:100]}) — charges is still authoritative; "
                          f"`spendguard migrate` rebuilds spend_events from charges.\n")


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


def record(provider, model, kind, cost, project=None, conv_id=None, basis=None,
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
    now = datetime.datetime.now(datetime.timezone.utc)
    with _lock:
        _db().execute("INSERT INTO charges "
                      "(ts,day,provider,model,kind,cost,project,conv_id,key_fp,basis,intent,actor) "
                      "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                      (now.isoformat(timespec="seconds"), now.strftime("%Y-%m-%d"),
                       provider or "?", model or "?", kind, float(cost), proj or "", conv or "", fp,
                       basis if basis in BASES else "", (intent or "")[:120], (actor or "")[:120]))
        _db().commit()
    # DUAL-WRITE the same charge into spend_events (the money-of-record we are cutting over to). charges above
    # stays authoritative until the readers are repointed; this keeps spend_events current so they can be.
    _shadow_spend_event(provider, model, kind, float(cost), conv_id=conv or "", basis=basis or "",
                        intent=intent or "", actor=actor or "", key_fp=fp, project=proj or "",
                        occurred_at=now.isoformat(timespec="seconds"), source="gate")


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
    with _lock:
        rows = _db().execute(
            "SELECT rowid, provider, model, cost FROM charges WHERE model IS NOT NULL AND model != ''"
        ).fetchall()
    fixes = []
    for rid, prov, model, cost in rows:
        true = gate._provider_of(model)
        # UNKNOWN IS NOT A CORRECTION. Overwriting a recorded vendor with "unknown" destroys information;
        # only a POSITIVE identification that disagrees is a fix.
        if true and true != gate.UNKNOWN_PROVIDER and prov and true != prov:
            fixes.append({"rowid": rid, "model": model, "was": prov, "now": true, "cost": float(cost or 0)})
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
    if apply and (fixes or True):
        # SNAPSHOT BEFORE WRITING. This function rewrote 697 rows the first time it ran, against a database
        # with no backup anywhere.
        _snap = snapshot(reason=f"reattribute-{actor}")
        if _snap:
            print(f"  ledger snapshot: {_snap}")
    if apply and fixes:
        with _lock:
            for f in fixes:
                stale = _wrong_fp.get(f["was"]) or ""
                if stale:
                    _db().execute("UPDATE charges SET provider=?, key_fp=CASE WHEN key_fp=? THEN '' "
                                  "ELSE key_fp END WHERE rowid=?", (f["now"], stale, f["rowid"]))
                else:
                    _db().execute("UPDATE charges SET provider=? WHERE rowid=?", (f["now"], f["rowid"]))
            _db().commit()
        # JOURNALLED AFTER THE COMMIT, AND THROUGH THE CHAIN. Two separate points:
        #   * the audit log has exactly ONE supported writer (SpendLedger.audit) — a raw INSERT here left
        #     697 rows with no row_hash and, until _audit was made tolerant, broke every audit write after
        #     them;
        #   * SpendLedger holds its OWN connection to the same file, so writing through it while this
        #     module's transaction is still open deadlocks on the write lock. The correction commits first.
        for f in fixes:
            try:
                _ledger().audit(str(f["rowid"]), actor, "reattribute", "provider", f["was"], f["now"],
                                f"model {f['model']} is served by {f['now']}, not {f['was']} "
                                f"(gate inferred the vendor from the model name prefix)")
            except Exception as e:
                unjournalled.append({"rowid": f["rowid"], "error": f"{type(e).__name__}: {str(e)[:60]}"})
        if unjournalled:
            # A CORRECTION WITH NO RECORD OF ITSELF IS THE THING THIS FUNCTION EXISTS TO PREVENT.
            import sys as _sys
            _sys.stderr.write(f"[budget] WARN {len(unjournalled)} of {len(fixes)} re-attributions COMMITTED "
                              f"WITHOUT AN AUDIT ROW — e.g. {unjournalled[0]}\n")
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
        with _lock:
            for rid, prov, fp in _db().execute(
                    "SELECT rowid, provider, key_fp FROM charges WHERE key_fp IS NOT NULL AND key_fp != ''"
            ).fetchall():
                owner = known.get(fp)
                if owner and prov and owner != prov:
                    stale.append({"rowid": rid, "provider": prov, "key_fp": fp, "belongs_to": owner})
        if apply and stale:
            with _lock:
                for st in stale:
                    _db().execute("UPDATE charges SET key_fp='' WHERE rowid=?", (st["rowid"],))
                _db().commit()
            for st in stale[:200]:                 # journalled like any other correction, capped for volume
                try:
                    _ledger().audit(str(st["rowid"]), actor, "reattribute", "key_fp", st["key_fp"], "",
                                    f"fingerprint belongs to {st['belongs_to']}, not {st['provider']} — "
                                    f"the key that served this call is not recoverable, so it is UNKNOWN")
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
    """Distinct repos (projects) THIS conversation touched — its workload charges. Powers the contextual receipt's
    collapsed view (a conversation can span repos; this very chat touched llm-spendguard + manga2anime + lmm)."""
    if not conv:
        return []
    with _lock:
        rows = _db().execute("SELECT DISTINCT project FROM charges WHERE conv_id = ? AND project != '' "
                             "AND (kind IS NULL OR kind != 'meta')", (str(conv),)).fetchall()
    return sorted(r[0] for r in rows if r[0])


def all_projects():
    """All repos with workload charges (the expanded all-repos view)."""
    with _lock:
        rows = _db().execute("SELECT DISTINCT project FROM charges WHERE project != '' "
                             "AND (kind IS NULL OR kind != 'meta') AND (model IS NULL OR model <> ?)",
                             (_RECONCILED,)).fetchall()
    return sorted(r[0] for r in rows if r[0])


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
    with _lock:
        db = _db()
        db.execute("DELETE FROM charges WHERE conv_id = ?", (conv,))
        for r in rows or []:
            cost = float(r.get("cost") or 0)
            if not cost:
                continue
            day = r.get("day") or datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")
            db.execute("INSERT INTO charges (ts,day,provider,model,kind,cost,project,conv_id) VALUES (?,?,?,?,?,?,?,?)",
                       (day + "T00:00:00+00:00", day, r.get("provider") or "?", r.get("model") or "?",
                        "remote", cost, proj, conv))
            n += 1; total += cost
        db.commit()
    # DUAL-WRITE (replace) into spend_events: drop this box's prior rows, then re-book the current ones.
    try:
        _ledger().delete(where={"conv_id": conv}, actor="ingest_remote", reason="re-sync remote box (replace)")
    except Exception:
        pass
    for i, r in enumerate(rows or []):
        cost = float(r.get("cost") or 0)
        if not cost:
            continue
        day = r.get("day") or datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")
        _shadow_spend_event(r.get("provider") or "?", r.get("model") or "?", "remote", cost, conv_id=conv,
                            project=proj, occurred_at=day + "T00:00:00+00:00", source="remote",
                            dedup_suffix=":%s:%d" % (conv, i))
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
                  "remote": ("remote", 0), "est_chat": ("est_chat", 0)}


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
        "cost_basis": cost_basis, "billed": 1,
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
    """Batch charges since `since`, joined to their `calls` row for the token counts, so an operator can SEE
    the arithmetic behind a number they doubt. Deliberately NOT automatic: recovering how many requests a
    past batch held is not always possible, and a repair that guesses the denominator would be the same class
    of mistake as the bug it is repairing. `spendguard quarantine --list` prints this; --ts acts on one row."""
    with _lock:
        # `calls` is created lazily (only when call logging is on), so its absence is NORMAL, not an error —
        # without it we still list the charges, just with no token counts to divide.
        has_calls = _db().execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='calls'").fetchone()
        # rowid is selected because it is the ONLY exact handle on a row: `ts` has second granularity and up
        # to six charges can share one second, so a ts-targeted repair could tag five innocent charges.
        sql = ("SELECT c.rowid, c.ts, c.day, c.provider, c.model, c.cost, COALESCE(c.project,''), "
               "       COALESCE(c.conv_id,''), k.in_tok, k.out_tok, COALESCE(k.caller,'') "
               "FROM charges c LEFT JOIN calls k ON k.ts = c.ts AND k.model = c.model AND k.kind = 'batch' "
               "WHERE c.kind='batch' AND c.day >= ? ORDER BY c.cost DESC") if has_calls else (
               "SELECT c.rowid, c.ts, c.day, c.provider, c.model, c.cost, COALESCE(c.project,''), "
               "       COALESCE(c.conv_id,''), NULL, NULL, '' "
               "FROM charges c WHERE c.kind='batch' AND c.day >= ? ORDER BY c.cost DESC")
        rows = _db().execute(sql, (since,)).fetchall()
    return [{"row": rid, "ts": t, "day": d, "provider": p_, "model": m, "cost": float(c or 0), "project": pr,
             "conv_id": cv, "in_tok": i, "out_tok": o, "caller": cl}
            for rid, t, d, p_, m, c, pr, cv, i, o, cl in rows]


def quarantine_charge(ts=None, reason="", row=None):
    """Tag ONE charge row as an impossible estimate. The row and its amount are untouched — only its conv_id
    marker changes, so it drops out of every total while staying fully auditable.

    Target by `row` (rowid — exact) or by `ts`. `charges.ts` has SECOND granularity and up to six charges can
    share one second, so a ts that matches more than one row RAISES rather than tagging all of them: silently
    excluding five innocent charges to quarantine one bad one would be a worse version of the bug this repairs.
    Returns the number of rows tagged (0 = nothing matched)."""
    with _lock:
        if row is not None:
            cur = _db().execute("UPDATE charges SET conv_id=? WHERE rowid=? AND conv_id <> ?",
                                (QUARANTINE_CONV, int(row), QUARANTINE_CONV))
        else:
            hits = _db().execute("SELECT rowid, model, cost FROM charges WHERE ts=? AND conv_id <> ?",
                                 (ts, QUARANTINE_CONV)).fetchall()
            if len(hits) > 1:
                raise ValueError(
                    "%d charges share the timestamp %s (%s) — refusing to quarantine all of them. Re-run with "
                    "--row <rowid> for the one you mean; `spendguard quarantine --list` shows the rowids."
                    % (len(hits), ts, ", ".join(f"row {h[0]}: {h[1]} ${h[2]:,.2f}" for h in hits)))
            cur = _db().execute("UPDATE charges SET conv_id=? WHERE ts=? AND conv_id <> ?",
                                (QUARANTINE_CONV, ts, QUARANTINE_CONV))
        n = cur.rowcount
        # THE MUTATION AND ITS AUDIT ROW COMMIT TOGETHER, UNDER THE SAME LOCK.
        #
        # This block used to sit OUTSIDE `with _lock:` and after its own commit, which is two defects. The
        # smaller one is the race: a concurrent writer can interleave between the mutation and its record.
        # The larger one is that the mutation was already durable, so a failed audit write left a changed
        # ledger with NO trace that anything changed it — and `except Exception: pass` meant nobody was
        # told. A missing audit row is indistinguishable from a mutation that never happened, which is the
        # exact failure this table exists to prevent: an accounting tool whose tamper record can go missing
        # in silence has no tamper record.
        _db().commit()
    # THROUGH THE CHAIN, AFTER THE COMMIT. The raw INSERT here left 99 unchained rows in the
    # tamper-evidence log — the same defect as reattribute's, and the one it was copied from — and
    # SpendLedger's own connection cannot write while this module's transaction is open.
    audit_err = None
    try:
        _ledger().audit(str(row if row is not None else ts), "quarantine_charge", "quarantine",
                        "conv_id", "", QUARANTINE_CONV, reason)
    except Exception as e:
        audit_err = e                          # audit shape varies by migration; never BLOCK the repair
    if audit_err is not None:
        # LOUD, not fatal. Blocking the repair because an older schema lacks the table would be a different
        # bug; letting the repair happen unrecorded and unmentioned is the one being fixed.
        import sys as _sys
        _sys.stderr.write(
            f"[budget] WARN quarantine of {'row ' + str(row) if row is not None else 'ts ' + str(ts)} "
            f"COMMITTED WITHOUT AN AUDIT ROW ({type(audit_err).__name__}: {str(audit_err)[:80]}). "
            f"The ledger changed and spend_audit does not know it — run `spendguard migrate` and re-check.\n")
    return n


def unpriced_since(day):
    """[{model, provider, calls, in_tok, out_tok}] of calls recorded with NO price since `day`. The tokens are
    real; only the dollars are unknown. Surfacing this is the difference between "we spent $0" and "we cannot
    tell you what we spent" — and the second one is actionable (`spendguard price <model> …`)."""
    with _lock:
        rows = _db().execute(
            "SELECT COALESCE(provider,'?'), COALESCE(model,'?'), COUNT(*) FROM charges "
            "WHERE day >= ? AND conv_id = ? GROUP BY 1, 2 ORDER BY 3 DESC", (day, UNPRICED_CONV)).fetchall()
    return [{"provider": p, "model": m, "calls": int(n)} for p, m, n in rows]


def record_unpriced(provider, model, kind, in_tok=0, out_tok=0, project=None):
    """Record that a call HAPPENED but could not be priced. cost=0 because we refuse to invent a number, but
    the row is MARKED so no total treats it as 'free' and the receipt can name the model to price."""
    now = datetime.datetime.now(datetime.timezone.utc)
    proj = (project if project is not None else _project()) or ""
    with _lock:
        _db().execute("INSERT INTO charges (ts,day,provider,model,kind,cost,project,conv_id,basis) "
                      "VALUES (?,?,?,?,?,?,?,?,?)",
                      (now.isoformat(timespec="seconds"), now.strftime("%Y-%m-%d"), provider or "?",
                       model or "?", kind, 0.0, proj, UNPRICED_CONV, BASIS_UNPRICED))
        _db().commit()
    # DUAL-WRITE: a $0 forensic marker (cost_basis='unpriced'), carrying the real tokens the price is unknown for.
    _shadow_spend_event(provider, model, kind, 0, basis=BASIS_UNPRICED, project=proj,
                        occurred_at=now.isoformat(timespec="seconds"), in_tok=in_tok, out_tok=out_tok,
                        source="gate", dedup_suffix=":unpriced")


# RAW-CHARGES-OK: listing quarantined rows IS the job — the view removes exactly these
def quarantined_since(day):
    """[{day, provider, model, cost, project}] of QUARANTINED rows since `day` — estimates the gate proved
    impossible. They are kept (forensics: what the estimator claimed, and when) and excluded from every total,
    so the honest thing is to SHOW them rather than let them vanish silently."""
    with _lock:
        rows = _db().execute(
            "SELECT day, COALESCE(provider,'?'), COALESCE(model,'?'), COALESCE(SUM(cost),0), COALESCE(project,''), "
            "COUNT(*) FROM charges WHERE day >= ? AND conv_id = ? GROUP BY day, provider, model, project "
            "ORDER BY SUM(cost) DESC", (day, QUARANTINE_CONV)).fetchall()
    return [{"day": d, "provider": p, "model": m, "cost": float(c or 0), "project": pr, "n": n}
            for d, p, m, c, pr, n in rows]


# RAW-CHARGES-OK: the audit breakdown must show every basis, including the excluded ones
def by_basis(day):
    """{basis: {'cost': $, 'n': rows}} since `day` — how much of the headline is a projection vs a bill.
    Unlabelled legacy rows come back under '' and are shown as unknown, never folded into 'billed'."""
    with _lock:
        rows = _db().execute(
            "SELECT COALESCE(basis,''), COALESCE(SUM(cost),0), COUNT(*) FROM charges WHERE day >= ? "
            "AND (kind IS NULL OR kind != 'meta') AND (model IS NULL OR model <> ?) "
            "AND (conv_id IS NULL OR conv_id NOT IN (?, ?)) GROUP BY 1",
            (day, _RECONCILED, QUARANTINE_CONV, UNPRICED_CONV)).fetchall()
    return {b: {"cost": float(c or 0), "n": int(n)} for b, c, n in rows}


# ── reconciliation: make the LOCAL ledger reflect PROVIDER-billed truth (the gap = ungoverned/pre-ledger spend) ──
def by_provider_day(kind=None, since=None):
    """Reads COUNTABLE_VIEW: this query used to keep its own exclusion list and was missing marker models, so a
    backfill row counted as period spend here too. {(provider, day): $} of GATE-recorded spend (excludes reconciled rows) — the attributed side of reconcile."""
    cond = ["(model IS NULL OR model <> ?)", "(conv_id IS NULL OR conv_id NOT IN (?, ?))"]
    args = [_RECONCILED, QUARANTINE_CONV, UNPRICED_CONV]           # like-for-like vs provider truth: quarantine excluded
    if kind:
        cond.append("kind=?"); args.append(kind)
    if since:
        cond.append("day >= ?"); args.append(since)
    where = "WHERE " + " AND ".join(cond)
    with _lock:
        rows = _db().execute(f"SELECT COALESCE(provider,'?'), day, COALESCE(SUM(cost),0) FROM {COUNTABLE_VIEW} {where} "
                             f"GROUP BY provider, day", args).fetchall()
    return {(p, d): float(c or 0) for p, d, c in rows}


# RAW-CHARGES-OK: reports the reconciliation marker rows the view excludes
def reconciled_by_project(since=None):
    """{project: $} of RECONCILED rows only (the provider-truth gap that reconcile_into_ledger attributed by
    conversation evidence). The 'attributed' side of the reconcile loop, complement to gate_by_project_day."""
    # A quarantined row carries a real model so it cannot match `model = _RECONCILED` — but the exclusion is
    # stated anyway rather than left as a fact someone has to re-derive. Cheap here, and the guard test
    # requires every cost aggregator to say it out loud.
    cond, args = ["model = ?", "(conv_id IS NULL OR conv_id NOT IN (?, ?))"], [_RECONCILED, QUARANTINE_CONV, UNPRICED_CONV]
    if since:
        cond.append("day >= ?"); args.append(since)
    where = "WHERE " + " AND ".join(cond)
    with _lock:
        rows = _db().execute(f"SELECT COALESCE(NULLIF(project,''),'unattributed'), COALESCE(SUM(cost),0) "
                             f"FROM charges {where} GROUP BY 1", args).fetchall()
    return {p: float(c or 0) for p, c in rows}


def gate_by_project_day(kind=None, since=None):
    """Reads COUNTABLE_VIEW: this query used to keep its own exclusion list and was missing marker models, so a
    backfill row counted as period spend here too. {(project, day): $} of GATE-recorded (attributed) spend — excludes reconciled rows. Used to compute the
    per-project gap so the provider-truth gap is attributed by evidence, not dumped in one 'unattributed' bucket."""
    cond = ["(model IS NULL OR model <> ?)", "(conv_id IS NULL OR conv_id NOT IN (?, ?))"]
    args = [_RECONCILED, QUARANTINE_CONV, UNPRICED_CONV]           # like-for-like vs provider truth: quarantine excluded
    if kind:
        cond.append("kind=?"); args.append(kind)
    if since:
        cond.append("day >= ?"); args.append(since)
    where = "WHERE " + " AND ".join(cond)
    with _lock:
        rows = _db().execute(f"SELECT COALESCE(NULLIF(project,''),'unattributed'), day, COALESCE(SUM(cost),0) "
                             f"FROM {COUNTABLE_VIEW} {where} GROUP BY 1, day", args).fetchall()
    return {(p, d): float(c or 0) for p, d, c in rows}


def record_reconciled(day, provider, cost, project="unattributed", kind="batch", model=None):
    """Insert a reconciliation row for provider-billed (batch) OR gate-logged (realtime) spend — the gap — attributed
    to `project` by evidence ('unattributed' only when there's none). Marked by a marker model so it's excluded from
    gate/cap and rebuilt idempotently. Default marker '(provider-batch)' / kind 'batch'; the realtime backfill passes
    its own marker + kind='realtime'."""
    marker = model or _RECONCILED
    with _lock:
        # basis=BILLED: a reconciliation row IS the provider's own number, not a projection of ours.
        _db().execute("INSERT INTO charges (ts,day,provider,model,kind,cost,project,basis) "
                      "VALUES (?,?,?,?,?,?,?,?)",
                      (day + "T00:00:00+00:00", day, provider or "?", marker, kind,
                       float(cost), project or "unattributed", BASIS_BILLED))
        _db().commit()
    # DUAL-WRITE: the marker model → reconciled=1 + recon_marker, so it's excluded from gate/cap like in charges.
    _shadow_spend_event(provider, marker, kind, float(cost), basis=BASIS_BILLED, project=project or "unattributed",
                        occurred_at=day + "T00:00:00+00:00", source="reconcile", dedup_suffix=":recon")


def clear_reconciled(since=None, model=None):
    """Remove prior reconciliation rows so reconcile is idempotent (rebuilds them). Keyed by the marker model
    (default the batch marker; the realtime backfill passes its own)."""
    snapshot_once("clear-reconciled")           # DELETEs money rows — never without a recovery path
    marker = model or _RECONCILED
    with _lock:
        if since:
            _db().execute("DELETE FROM charges WHERE model=? AND day >= ?", (marker, since))
        else:
            _db().execute("DELETE FROM charges WHERE model=?", (marker,))
        _db().commit()


# ── estimate→actual true-down (ledger_sync.true_down writes these; the gate's batch rows are PRE-SUBMIT
#    estimates, so once the provider bills the actuals the ledger must come down to the billed truth) ──
# RAW-CHARGES-OK: excludes true-down as well, which the view deliberately keeps
def gate_batch_cells(since=None):
    """{(project, provider, model, day): $} of GATE-LIVE batch rows — the estimate base the true-down corrects.
    Excludes every reconcile marker model AND prior true-down rows (idempotence: corrections never feed the next
    correction). Full-dimension sibling of gate_by_project_day."""
    cond = ["kind='batch'", f"(model IS NULL OR model NOT IN ({','.join('?' * len(_MARKER_MODELS))}))",
            "(conv_id IS NULL OR conv_id NOT IN (?, ?, ?))"]
    args = list(_MARKER_MODELS) + [_TRUE_DOWN_CONV, QUARANTINE_CONV, UNPRICED_CONV]
    if since:
        cond.append("day >= ?"); args.append(since)
    with _lock:
        rows = _db().execute("SELECT COALESCE(NULLIF(project,''),'unattributed'), COALESCE(provider,'?'), "
                             "COALESCE(model,'?'), day, COALESCE(SUM(cost),0) FROM charges WHERE "
                             + " AND ".join(cond) + " GROUP BY 1, 2, 3, day", args).fetchall()
    return {(pr, p, m, d): float(c or 0) for pr, p, m, d, c in rows}


def record_true_down(day, provider, model, delta, project):
    """Insert ONE negative batch correction row: estimate − billed for this cell's share. Carries the REAL model
    (so by_dims nets it against the estimate rows before the SaaS push) and the true-down conv_id sentinel (so it
    is identifiable/clearable without a marker model). The original estimate rows are NEVER mutated — the ledger
    keeps both the estimate and the correction (forensic: what we thought + what it actually billed)."""
    with _lock:
        _db().execute("INSERT INTO charges (ts,day,provider,model,kind,cost,project,conv_id) VALUES (?,?,?,?,?,?,?,?)",
                      (day + "T00:00:00+00:00", day, provider or "?", model or "?", "batch",
                       -abs(float(delta)), project or "unattributed", _TRUE_DOWN_CONV))
        _db().commit()
    # DUAL-WRITE: a NEGATIVE batch row carrying the REAL model + the true-down conv sentinel (nets the estimate down).
    _shadow_spend_event(provider, model, "batch", -abs(float(delta)), conv_id=_TRUE_DOWN_CONV,
                        project=project or "unattributed", occurred_at=day + "T00:00:00+00:00",
                        source="true-down", dedup_suffix=":td")


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
    with _lock:
        if since:
            _db().execute("DELETE FROM charges WHERE conv_id=? AND day >= ?", (_TRUE_DOWN_CONV, since))
        else:
            _db().execute("DELETE FROM charges WHERE conv_id=?", (_TRUE_DOWN_CONV,))
        _db().commit()


# ── spendguard's own advisor LLM use (segregated: own cap, own line, excluded from workload) ──
def record_meta(provider, model, cost):
    # spendguard's OWN spend → the llm-spendguard project, kept distinct by kind='meta' (NOT a separate project tag).
    record(provider, model, "meta", cost, project="llm-spendguard")


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
    # Quarantined rows are excluded UNCONDITIONALLY, in every caller. They are estimates the gate proved
    # impossible; counting them as "accounted" is what let an invented $54.51 sit inside a leak check that
    # then reported no leak. A row that cannot describe a real request is not coverage of anything.
    cond, args = ["(conv_id IS NULL OR conv_id NOT IN (?, ?))"], [QUARANTINE_CONV, UNPRICED_CONV]
    if kind:
        cond.append("kind=?"); args.append(kind)
    if exclude_meta:
        cond.append("(kind IS NULL OR kind != 'meta')")
    if exclude_reconciled:
        cond.append(f"(model IS NULL OR model NOT IN ({','.join('?' * len(_MARKER_MODELS))}))")
        args.extend(_MARKER_MODELS)
    if since:
        cond.append("day >= ?"); args.append(since)
    where = "WHERE " + " AND ".join(cond)
    with _lock:
        rows = _db().execute(f"SELECT day, COALESCE(SUM(cost),0) FROM charges {where} GROUP BY day", args).fetchall()
    return {d: float(v or 0) for d, v in rows}


# RAW-CHARGES-OK: the SaaS PUSH payload deliberately carries backfill AND meta rows — the org
# dashboard needs them. countable_charges strips both, so routing this through it silently
# changed what we ship upstream; tests/test_ledger_marker_matrix.py caught it.
def by_dims(since=None):
    """Per-day rows grouped by (day, provider, model, kind) for the SaaS roll-up push — the structured shape
    the server's /v1/ledger expects (vs by_day's flat {day: $}). Returns dicts with cost in $ and a call count."""
    # This is the SaaS PUSH payload. A quarantined row here would put an invented number on the org
    # dashboard, where nobody has the local context to question it.
    cond, args = ["(conv_id IS NULL OR conv_id NOT IN (?, ?))"], [QUARANTINE_CONV, UNPRICED_CONV]
    if since:
        cond.append("day >= ?"); args.append(since)
    where = "WHERE " + " AND ".join(cond)
    with _lock:
        rows = _db().execute(
            f"SELECT day, COALESCE(provider,'?'), COALESCE(model,'?'), COALESCE(kind,'workload'), "
            f"COALESCE(project,''), COALESCE(SUM(cost),0), COUNT(*) FROM charges {where} "
            f"GROUP BY day, provider, model, kind, project", args
        ).fetchall()
    return [dict(day=d, provider=p, model=m, kind=k, project=pr, cost=float(c or 0), calls=int(n)) for d, p, m, k, pr, c, n in rows]


# RAW-CHARGES-OK: excludes true-down as well, which the view deliberately keeps
def by_key(since=None):
    """{(provider, key_fp): {'cost': $, 'calls': n}} of gate-recorded workload — per-KEY spend (which
    workspace/project key did this money flow through?). Excludes meta and every reconcile marker row
    (mirror/backfill rows carry no key). key_fp '' groups as '(none)' — rows recorded before key stamping
    existed, or where no key env was resolvable. LOCAL-ONLY view; fingerprints never leave the machine."""
    cond = ["(kind IS NULL OR kind != 'meta')",
            f"(model IS NULL OR model NOT IN ({','.join('?' * len(_MARKER_MODELS))}))",
            "(conv_id IS NULL OR conv_id NOT IN (?, ?, ?))"]
    args = list(_MARKER_MODELS) + [_TRUE_DOWN_CONV, QUARANTINE_CONV, UNPRICED_CONV]
    if since:
        cond.append("day >= ?"); args.append(since)
    with _lock:
        rows = _db().execute("SELECT COALESCE(provider,'?'), COALESCE(NULLIF(key_fp,''),'(none)'), "
                             "COALESCE(SUM(cost),0), COUNT(*) FROM charges WHERE "
                             + " AND ".join(cond) + " GROUP BY 1, 2", args).fetchall()
    return {(p, fp): {"cost": float(c or 0), "calls": int(n)} for p, fp, c, n in rows}


def ledger_start(kind=None):
    """Earliest day in the local ledger — spend before this wasn't recorded locally (pre-ledger). With `kind`,
    the earliest day for THAT axis: axes start recording at different times (realtime began long before batch),
    so a per-axis cutoff is what keeps the leak check from mislabeling one axis's pre-history as a leak."""
    with _lock:
        if kind:
            r = _db().execute("SELECT MIN(day) FROM charges WHERE kind=?", (kind,)).fetchone()
        else:
            r = _db().execute("SELECT MIN(day) FROM charges").fetchone()
    return r[0] if r and r[0] else None


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

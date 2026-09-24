"""TEST-FIRST + ESTIMATE-FIRST enforcement — make it structurally impossible to run a BULK paid LLM job without a
zero-spend ESTIMATE and a verified small-sample TEST. The protocol used to exist only as discipline and got skipped
(a real consumer's opus escalation spent ~$5.61 unestimated + untested, then crashed). This makes the gate BLOCK instead.

How: three flags — `estimated`, `tested`, and `eval-passed` — attach to a CALL-CLASS SIGNATURE (model + template +
schema), persist in sqlite (survive a fresh `python`), and `check_bulk` REFUSES a scale submit whose sig lacks FRESH
flags. The only path to a full paid run becomes estimate → test (parse the shape) → EVAL (an LLM judges the sample
against a STATED bar) → run. `model` is part of the sig, so testing Haiku never authorizes Opus/nano; changing the
prompt/schema changes the sig → must re-test (no "tested v1, ran v2").

Surface: record_estimate · record_tested · record_eval · eval_job (the AGENTIC quality checkpoint) · check_bulk
(raises GateBlocked) · status · sig · gated_batch (the ordered unblock wrapper). The eval VERDICT is a judgement (an
LLM decides it); the gate's CHECK ("does a fresh passing eval exist?") is the only mechanical part. Rollout via
SPENDGUARD_ENFORCE = off | warn | block (default `warn` — log "would-block" — then `block`); the eval requirement is
`gate.require_eval` (default on).
"""
import os
import time
import json
import hashlib
import sqlite3
import threading
import contextlib
from . import config
from .gate import SpendGateRefused   # the common base for DELIBERATE gate refusals (see GateBlocked below).
# Import direction: gate depends on bulkgate but only LAZILY (inside functions), and bulkgate imports nothing
# else from gate — so this top-level import is acyclic (gate always finishes loading before bulkgate here).

PREVIEW_MAX_DEFAULT = 25          # a run of <= this many requests is a PREVIEW/TEST — allowed WITHOUT flags (it IS the test)
BULK_MIN_USD_DEFAULT = 0.25       # $-primary trigger DEFAULT (tunable: gate.bulk_min_usd): below this estimated cost,
                                  # no enforcement (trivial / a single small call). At/above it AND multi-unit (count >
                                  # preview_max, so there IS a sample to test+eval) the lifecycle gate engages.
FRESHNESS_HOURS_DEFAULT = 24      # flags expire — a stale test can't authorize a much-later run on changed data

_lock = threading.RLock()


class GateBlocked(SpendGateRefused):
    """Raised when a BULK paid run is attempted without a FRESH estimate+test for its call-class signature.

    Subclasses SpendGateRefused ON PURPOSE: it is a DELIBERATE refusal, not a gate malfunction. Every fail-open
    handler in the gate (`_guard` and the pre/post-call wrappers) catches the CONCEPT — `except SpendGateRefused`
    — and re-raises it, swallowing only genuine malfunctions (`database is locked`, a buggy register()'d fn). If
    GateBlocked did NOT share that base (it used to be `Exception`), those handlers would treat this block as a
    malfunction and let the un-estimated batch through — which is exactly the bug this inheritance closes. Any new
    deliberate-refusal type must likewise subclass SpendGateRefused so it blocks by CONSTRUCTION, never by an
    enumerated `except (A, B, …)` list that someone has to remember to extend."""


def _ensure_gate_schema(c):
    """Create the gate_ledger table (+ its forward-only additive columns). Idempotent; run once per pooled connection
    by config.pooled_ledger_conn. The gate_calls / gate_latency tables are ensured by _gate_calls_db() on the same
    (now pooled) connection."""
    c.execute(
        "CREATE TABLE IF NOT EXISTS gate_ledger ("
        " sig TEXT PRIMARY KEY, model TEXT,"
        " estimated_at REAL, est_usd REAL, est_count INTEGER,"   # worst-case estimate (incl. escalation)
        " tested_at REAL, test_n INTEGER, verified INTEGER,"     # a verified small-sample run happened
        " updated_at REAL)")
    # Additive, forward-only. `verified` alone said a test HAPPENED; these say what it PROVED —
    # which contract the output was checked against, on which data, and what the sample did.
    # The eval_* columns add the LIFECYCLE checkpoint ABOVE the shape-test: a STATED bar + an AGENTIC
    # verdict on the sample. eval_bar is REQUIRED non-empty (an eval can't be an empty rubber-stamp);
    # eval_verdict is the pass/fail; eval_score/eval_note carry the judge's graded reasoning; eval_model
    # records WHICH model judged (honesty). Test = "did it parse the shape"; eval = "is it GOOD".
    for col, decl in (("contract", "TEXT"), ("contract_hash", "TEXT"), ("data_sig", "TEXT"),
                      ("test_parsed", "INTEGER"), ("test_salvaged", "INTEGER"),
                      ("test_failed", "INTEGER"), ("test_failure", "TEXT"),
                      ("eval_at", "REAL"), ("eval_bar", "TEXT"), ("eval_verdict", "INTEGER"),
                      ("eval_score", "REAL"), ("eval_note", "TEXT"), ("eval_model", "TEXT")):
        try:
            c.execute(f"ALTER TABLE gate_ledger ADD COLUMN {col} {decl}")
        except sqlite3.OperationalError:
            pass                                  # already present
    c.commit()


def _gate_db():
    """The gate_ledger table, on the shared pooled ledger connection (config.pooled_ledger_conn, keyed 'bulkgate') —
    reused, tuned, fork-safe. `_lock` still serializes the module's read-modify-write sites; _gate_calls_db() ensures
    gate_calls / gate_latency on this same connection."""
    return config.pooled_ledger_conn("bulkgate", _ensure_gate_schema)


# ── config (env > config.json gate.<name> > default) ──
def _gate_setting(name, default, cast):
    v = os.getenv("SPENDGUARD_" + name.upper())
    if v is None:
        try:
            v = config._cfg_get("gate", name, None)
        except Exception:
            v = None
    try:
        return cast(v) if v is not None else default
    except (TypeError, ValueError):
        return default


def preview_max():
    return _gate_setting("preview_max", PREVIEW_MAX_DEFAULT, int)


def bulk_min_usd():
    return _gate_setting("bulk_min_usd", BULK_MIN_USD_DEFAULT, float)


def freshness_hours():
    return _gate_setting("freshness_hours", FRESHNESS_HOURS_DEFAULT, float)


def require_eval():
    """Does a gated scale run additionally require a FRESH PASSING eval (a stated bar + an agentic verdict on the
    sample), on top of estimate+test? Default TRUE — the eval is the lifecycle's quality checkpoint. Set
    `gate.require_eval=false` (or SPENDGUARD_REQUIRE_EVAL=0) to fall back to the estimate+test-only gate. Config, not
    a hardcode: a repo that has not yet adopted evals can keep the older gate while it does."""
    v = os.getenv("SPENDGUARD_REQUIRE_EVAL")
    if v is not None:
        return v.strip().lower() not in ("0", "false", "no", "off")
    return config._cfg_get("gate", "require_eval", True) is not False


def mode():
    """Roll-out switch: off | warn | block. Default `warn` (log "would-block" so consumers see what's coming) — flip to
    `block` once they've adopted estimate/test. `enforce_test_first=false` in config forces `off`."""
    if config._cfg_get("gate", "enforce_test_first", True) is False:
        return "off"
    return (os.getenv("SPENDGUARD_ENFORCE") or config._cfg_get("gate", "enforce", None) or "warn").lower()


def sig(model, template_id=None, template_version=None, schema_name=None, prompt=None):
    """Stable id for a CLASS of paid work — flags attach to the WORK, not one request. `model` is ALWAYS part of it
    (testing Haiku must not authorize Opus/nano). Consumer supplies template_id/version/schema; fallback = a hash of
    model + the first 512 chars of the prompt (changing the prompt template → new sig → must re-test)."""
    if template_id or template_version or schema_name:
        key = "|".join(str(x or "") for x in (model, template_id, template_version, schema_name))
    else:
        # THE WHOLE PROMPT. Truncating to 512 chars made every prompt sharing a preamble collide into one
        # signature — and in this codebase a shared preamble is the NORM: system blocks, review briefs and
        # compacted-source headers all run past 512 characters before the part that differs. Two different
        # calls then shared one class, so measured caps, latencies and cache entries from one were served
        # to the other. Hashing costs the same whatever the length.
        key = (model or "") + "|" + (prompt or "")
    return hashlib.sha256(key.encode()).hexdigest()[:16]


def _fresh(ts):
    return bool(ts) and (time.time() - float(ts)) <= freshness_hours() * 3600


def record_estimate(sig, model, est_usd, est_count):
    """Record a ZERO-SPEND worst-case estimate for this call-class (sets estimated_at). WORST-CASE incl. any
    escalation path — not the cheap path (the nano-only estimate that hid the $5.61 opus run is the cautionary tale)."""
    now = time.time()
    with _lock:
        _gate_db().execute(
            "INSERT INTO gate_ledger (sig,model,estimated_at,est_usd,est_count,updated_at) VALUES (?,?,?,?,?,?) "
            "ON CONFLICT(sig) DO UPDATE SET model=excluded.model, estimated_at=excluded.estimated_at, "
            "est_usd=excluded.est_usd, est_count=excluded.est_count, updated_at=excluded.updated_at",
            (sig, model, now, float(est_usd), int(est_count), now))
        _gate_db().commit()
    # FORWARD IT TO THE LEARNED ESTIMATOR. This wrote gate_ledger and stopped. calibrate.pair() reads
    # cost_predictions and NOTHING bridged the two, so every estimate spendguard's own gate recorded was
    # invisible to spendguard's own calibrator — which then trained only on predictions an external consumer
    # remembered to log by hand through a DIFFERENTLY NAMED function. Neither module was wrong on its own;
    # the defect was the gap. Never raises: a calibration write must not be able to block an authorization.
    try:
        from . import calibrate
        calibrate.record_prediction(sig, f"gate:{sig[:12]}", model, float(est_usd), n=int(est_count),
                                    transport="batch")
    except Exception:
        pass
    return now


def record_tested(sig, test_n, verified=True, contract=None, result=None, data_sig=None):
    """Record a small-sample test AND what it proved: which output CONTRACT the sample was checked against, on
    which data (`data_sig`), and how the sample actually did (`result` from output_contract.check).

    `verified` used to mean only "a test ran". It now means "the output matched the declared shape", which is
    the claim a bulk run is actually relying on."""
    from . import output_contract
    now = time.time()
    desc = output_contract.describe(contract) if contract is not None else ""
    chash = output_contract.contract_hash(contract) if contract is not None else ""
    r = result.as_dict() if result is not None else {"parsed": 0, "salvaged": 0, "failed": 0, "first_failure": ""}
    with _lock:
        _gate_db().execute(
            "INSERT INTO gate_ledger (sig,tested_at,test_n,verified,contract,contract_hash,data_sig,"
            " test_parsed,test_salvaged,test_failed,test_failure,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(sig) DO UPDATE SET tested_at=excluded.tested_at, test_n=excluded.test_n, "
            "verified=excluded.verified, contract=excluded.contract, contract_hash=excluded.contract_hash, "
            "data_sig=excluded.data_sig, test_parsed=excluded.test_parsed, test_salvaged=excluded.test_salvaged, "
            "test_failed=excluded.test_failed, test_failure=excluded.test_failure, updated_at=excluded.updated_at",
            (sig, now, int(test_n), int(bool(verified)), desc, chash, data_sig or "",
             int(r["parsed"]), int(r["salvaged"]), int(r["failed"]), str(r["first_failure"])[:300], now))
        _gate_db().commit()
    return now


def record_eval(sig, bar, verdict, score=None, note=None, model=None):
    """Record the EVAL checkpoint for this call-class: a verdict on the test sample against a STATED bar. A bar is
    REQUIRED — an eval with no criterion is an empty rubber-stamp, so an empty bar is REFUSED rather than recorded as
    a meaningless pass. `verdict` is the pass/fail an LLM judge returned (see eval_job — the verdict is a JUDGEMENT,
    made agentically, never by keyword here); score/note carry its graded reasoning; `model` records WHO judged
    (honesty). Same sig as estimate+test — one intent, one row."""
    bar = (bar or "").strip()
    if not bar:
        raise ValueError("eval needs a STATED bar (what a passing output looks like) — an eval without a bar is an "
                         "empty rubber-stamp and cannot authorize a run. Pass bar='...'.")
    now = time.time()
    with _lock:
        _gate_db().execute(
            "INSERT INTO gate_ledger (sig,eval_at,eval_bar,eval_verdict,eval_score,eval_note,eval_model,updated_at) "
            "VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(sig) DO UPDATE SET eval_at=excluded.eval_at, "
            "eval_bar=excluded.eval_bar, eval_verdict=excluded.eval_verdict, eval_score=excluded.eval_score, "
            "eval_note=excluded.eval_note, eval_model=excluded.eval_model, updated_at=excluded.updated_at",
            (sig, now, bar, int(bool(verdict)), (float(score) if score is not None else None),
             (str(note)[:500] if note else ""), (model or ""), now))
        _gate_db().commit()
    return now


def gate_status(sig, contract=None, data_sig=None):
    """{estimated, tested, verified, fresh, contract, …} for this sig — freshness-aware.

    Pass the CURRENT `contract` / `data_sig` and freshness additionally requires that they MATCH what was
    tested. A test proves something about a shape and a data distribution; carrying it over to a different
    shape or a different corpus is the "tested v1, ran v2" hole the sig already closes for the prompt."""
    from . import output_contract
    with _lock:
        r = _gate_db().execute("SELECT model,estimated_at,est_usd,est_count,tested_at,test_n,verified,"
                          "contract,contract_hash,data_sig,test_parsed,test_salvaged,test_failed,test_failure,"
                          "eval_at,eval_bar,eval_verdict,eval_score,eval_note "
                          "FROM gate_ledger WHERE sig=?", (sig,)).fetchone()
    if not r:
        return {"sig": sig, "estimated": False, "tested": False, "verified": False, "eval_ok": False,
                "eval_required": require_eval(), "fresh": False,
                "contract": "", "contract_match": False, "data_match": False,
                "reason": "never estimated or tested"}
    est_ok, test_ok = _fresh(r[1]), _fresh(r[4])
    want_c = output_contract.contract_hash(contract) if contract is not None else None
    c_match = True if want_c is None else (want_c == (r[8] or ""))
    d_match = True if not data_sig else (str(data_sig) == (r[9] or ""))
    # EVAL: a fresh, PASSING verdict against a STATED bar. Folded into `fresh` only when require_eval() is on, so a
    # repo still on the estimate+test gate is unaffected; eval_ok is reported either way so the caller can see it.
    eval_fresh, eval_bar, eval_verdict = _fresh(r[14]), (r[15] or ""), bool(r[16])
    eval_ok = eval_fresh and eval_verdict and bool(eval_bar)
    need_eval = require_eval()
    fresh = est_ok and test_ok and bool(r[6]) and c_match and d_match and (eval_ok or not need_eval)
    reason = ("" if fresh else
              "estimate stale/missing" if not est_ok else
              "test stale/missing" if not test_ok else
              "the sample output did NOT match the declared contract" if not r[6] else
              "the output contract CHANGED since the test" if not c_match else
              "the test ran on DIFFERENT data" if not d_match else
              "no eval yet — a STATED bar + an agentic verdict on the sample is required" if need_eval and not eval_fresh else
              "the eval FAILED its stated bar (a failing eval keeps scale blocked until a passing one exists)"
              if need_eval and not eval_verdict else
              "the eval has no STATED bar" if need_eval and not eval_bar else "")
    return {"sig": sig, "model": r[0], "estimated": est_ok, "est_usd": r[2], "est_count": r[3],
            "tested": test_ok, "test_n": r[5], "verified": bool(r[6]), "fresh": fresh,
            "contract": r[7] or "", "contract_match": c_match, "data_match": d_match, "data_sig": r[9] or "",
            "parsed": r[10] or 0, "salvaged": r[11] or 0, "failed": r[12] or 0, "failure": r[13] or "",
            "eval_ok": eval_ok, "eval_required": need_eval, "eval_bar": eval_bar, "eval_verdict": eval_verdict,
            "eval_score": r[17], "eval_note": r[18] or "", "eval_fresh": eval_fresh,
            "reason": reason}


def _log_block(sig, model, count, est_usd, decision):
    """Telemetry — every block / would-block / override is logged (so the receipt can show 'M blocked', and overrides
    are never silent). Appended to a jsonl in spendguard's home; also a stderr line."""
    import sys
    rec = {"ts": time.time(), "sig": sig, "model": model, "count": count, "est_usd": round(float(est_usd or 0), 4),
           "decision": decision}
    try:
        with open(os.path.join(os.path.dirname(config.db_path()), "gate_blocks.jsonl"), "a") as f:
            f.write(json.dumps(rec) + "\n")
    except Exception:
        pass
    print("[bulkgate] %s %s (%s): %d reqs ~$%.2f without a fresh %s"
          % (decision.upper(), sig, model, count, float(est_usd or 0),
             "estimate+test+eval" if require_eval() else "estimate+test"), file=sys.stderr)


def check_bulk(sig, model, count, est_usd, force=False, contract=None, data_sig=None):
    """Call BEFORE a bulk submit. RAISES GateBlocked if this call-class lacks a FRESH estimate+verified-test — UNLESS:
      • it's a PREVIEW (count <= preview_max AND est_usd <= bulk_min_usd) — that IS the allowed test step,
      • mode is `off` (enforcement disabled), or `warn` (logs 'would-block' but allows — the roll-out grace period),
      • force=True or env GATE_FORCE=1 — an explicit, LOGGED human override (never a silent bypass).
    Returns the decision string ('preview'|'pass'|'allow:<mode/force>'); raises only in `block` mode without flags."""
    pm, bm = preview_max(), bulk_min_usd()
    if count <= pm and float(est_usd or 0) <= bm:
        return "preview"                                          # the test step itself — always allowed
    if gate_status(sig, contract=contract, data_sig=data_sig)["fresh"]:
        return "pass"                             # fresh estimate + contract-verified test on THIS data → authorized
    forced = bool(force) or os.getenv("GATE_FORCE") == "1"
    m = mode()
    if m == "off":
        return "allow:off"
    if forced:
        _log_block(sig, model, count, est_usd, "override")
        return "allow:force"
    if m == "warn":
        _log_block(sig, model, count, est_usd, "would-block")
        return "allow:warn"
    _log_block(sig, model, count, est_usd, "blocked")
    st = gate_status(sig, contract=contract, data_sig=data_sig)
    detail = ""
    if st.get("failed"):
        detail = (" The last sample FAILED the contract: %d/%d items — %s."
                  % (st["failed"], (st.get("test_n") or 0), st.get("failure") or "?"))
    elif st.get("salvaged"):
        detail = (" The last sample only parsed after stripping a fence/preamble (%d items) — fix the prompt or "
                  "widen the contract." % st["salvaged"])
    raise GateBlocked(
        "BLOCKED %s (%s): scale run of %d (~$%.2f) needs estimate+test+eval FIRST — %s "
        "(estimated=%s tested=%s contract-verified=%s eval=%s). Run estimate_job(sig, model, worst_case_usd, count), "
        "a <=%d-item test_job(sig, run_fn, contract=[...], items=[...]), then eval_job(sig, bar='what a passing "
        "output looks like', sample=test_outputs), then re-run.%s Override (logged): GATE_FORCE=1."
        % (sig, model, count, float(est_usd or 0), st.get("reason") or "not authorized",
           st["estimated"], st["tested"], st["verified"],
           ("pass" if st.get("eval_ok") else "MISSING/FAIL") if st.get("eval_required") else "n/a", pm, detail))


# ── max_tokens: truncation DETECTION (the API states it — a fact, not a guess) + data-driven bounds (measure the
#    output distribution) — keyed by the SAME call-class sig. The single chokepoint sees both the request's max_tokens
#    and the response usage, so this protects every repo with zero per-repo work. ──
def _gate_calls_db():
    db = _gate_db()
    db.execute("CREATE TABLE IF NOT EXISTS gate_calls "
               "(sig TEXT, model TEXT, out_tok INTEGER, max_tokens INTEGER, truncated INTEGER, ts REAL)")
    db.execute("CREATE INDEX IF NOT EXISTS idx_gatecalls_sig ON gate_calls(sig)")
    db.execute("CREATE INDEX IF NOT EXISTS idx_gatecalls_model ON gate_calls(model)")  # model-level fill obs (calibrate)
    # TIME is the second termination bound, and it had no measurement at all. Same shape as out_tok: record
    # what actually happened per call-class so a deadline can be sized rather than guessed.
    db.execute("CREATE TABLE IF NOT EXISTS gate_latency "
               "(sig TEXT, model TEXT, seconds REAL, hit_deadline INTEGER, ts REAL)")
    db.execute("CREATE INDEX IF NOT EXISTS idx_gatelat_sig ON gate_latency(sig, model)")
    # THE PAYLOAD SIZE IS WHAT DRIVES THE LATENCY, so it is recorded with it. Without this column a 5,000
    # char prompt and a 37,000 char one are one population: measured on kimi-k3 across review calls, p50 was
    # 38s and p95 was 289s, and that spread IS the size effect. A budget drawn from the mixed distribution
    # is simultaneously far too generous for the small calls and fatal for the large ones — kimi-k3 needed
    # 547s for a 37,453-char file and was killed at the mixed-population budget, losing that file's review
    # entirely while three other vendors reported on it.
    if "in_chars" not in [r[1] for r in db.execute("PRAGMA table_info(gate_latency)")]:
        db.execute("ALTER TABLE gate_latency ADD COLUMN in_chars INTEGER DEFAULT 0")
    return db


def is_truncated(finish_reason, out_tok=None, max_tokens=None):
    """Did the response get CUT OFF at the cap? The API says so: Anthropic stop_reason=='max_tokens', OpenAI
    finish_reason=='length'. Belt-and-suspenders: out_tok hitting max_tokens exactly. A fact, not a guess."""
    if (finish_reason or "").lower() in ("length", "max_tokens"):
        return True
    return bool(out_tok and max_tokens and int(out_tok) >= int(max_tokens))


def note_latency(sig, model, seconds, hit_deadline=False, in_chars=0):
    """Record how LONG one call took, keyed by call-class — the time analogue of note_response's out_tok.

    Without this a deadline is a guess, and a guessed deadline fails the same way a guessed max_tokens does:
    too low and the call dies after you have already paid for the input, too high and you wait three minutes
    to learn a vendor is down. Measured across four vendors on identical prompts, p90 latency ranged 20.6s to
    116.8s — a single global 180s was simultaneously far too generous for one and marginal for another."""
    try:
        with _lock:
            _gate_calls_db().execute(
                "INSERT INTO gate_latency (sig,model,seconds,hit_deadline,ts,in_chars) "
                "VALUES (?,?,?,?,?,?)",
                (sig, model, float(seconds or 0), int(bool(hit_deadline)), time.time(),
                 int(in_chars or 0)))
            _gate_db().commit()
    except Exception:
        pass


# Size band for "comparable payload", as a RATIO rather than absolute char counts: latency scales with the
# payload, so what makes two observations comparable is being within the same factor of each other, not
# within some number of characters. Stated here, applied in one place, and reported in the basis so a
# reader can see which population a budget came from.
_SIZE_BAND = 2.5

# How many COMPLETED observations a size band needs before it may override the whole-population answer.
# Below this a band is noise wearing a narrower label, and a narrower label reads as more precise.
MIN_LATENCY_OBS = 5


def latency(sig=None, model=None, near_chars=None):
    """{n, p50, p90, p99, max, deadline_hits, hit_rate} for a class and/or model, or {} if nothing measured.

    CENSORING, same rule as maxtokens(): a call that hit its deadline was cut AT the budget, so it measures
    the BUDGET, not the work. Including those would drag every percentile toward whatever budget happened to
    be set — and the tighter the budget, the lower the 'measured' latency, which is a ratchet that recommends
    ever-shorter deadlines the more calls it kills. Timed-out calls are counted and set a FLOOR instead."""
    where, args = [], []
    if sig:
        where.append("sig=?"); args.append(sig)
    if model:
        where.append("model=?"); args.append(model)
    band = None
    if near_chars:
        # Comparable payloads only. Falls back to the whole population when the band is too thin to say
        # anything — and REPORTS which it used, because a budget drawn from 3 observations of the right
        # size and one drawn from 300 of the wrong size are different claims.
        band = (int(near_chars / _SIZE_BAND), int(near_chars * _SIZE_BAND))
    q = ("SELECT seconds,hit_deadline,COALESCE(in_chars,0) FROM gate_latency"
         + (" WHERE " + " AND ".join(where) if where else ""))
    try:
        with _lock:
            rows = _gate_calls_db().execute(q, args).fetchall()
    except Exception:
        return {}
    scope = "all-sizes"
    if band:
        sized = [r for r in rows if r[2] and band[0] <= r[2] <= band[1]]
        if len([r for r in sized if not r[1] and r[0] > 0]) >= MIN_LATENCY_OBS:
            rows, scope = sized, f"~{near_chars:,}c"
    done = [r[0] for r in rows if not r[1] and r[0] > 0]
    hits = [r[0] for r in rows if r[1]]
    if not done:
        return ({"n": 0, "scope": scope, "deadline_hits": len(hits),
                 "hit_rate": 1.0 if hits else 0.0,
                 "floor": max(hits) if hits else None} if hits else {})
    # FLOAT percentiles. _pctl is built for TOKEN COUNTS and returns an int, which silently destroys this
    # measurement twice over: sub-second latencies all become 0, and a p99 of 0 is FALSY — so a caller
    # testing `if d.get("p99")` skips the class rung and quietly falls back to the model-wide number without
    # saying so. Observed as "p50: 0, p90: 7, p99: 10" on a class whose calls really took fractions of a
    # second. Seconds are not tokens; they need the fractional part.
    def _sec(vals, q):
        """Seconds at quantile q. Same single algorithm as _pctl — this was a nearest-rank variant whose
        index `int(len(v)*q)` overshoots by up to one position (q=1.0 indexes len, out of range) and which
        raised IndexError on an empty sample rather than saying it did not know."""
        from .calibrate import _quantile
        s = _quantile(vals, q)
        return None if s is None else round(float(s), 3)

    return {"n": len(done), "scope": scope,
            "p50": _sec(done, 0.50), "p90": _sec(done, 0.90), "p95": _sec(done, 0.95),
            "p99": _sec(done, 0.99), "max": max(done), "deadline_hits": len(hits),
            "hit_rate": len(hits) / float(len(rows)) if rows else 0.0,
            "floor": max(hits) if hits else None}


def note_response(sig, model, out_tok, max_tokens=None, finish_reason=None):
    """Record one response's output size + whether it TRUNCATED, keyed by call-class sig. Truncation → loud warning
    (you paid for input + a cut-off output and got corrupt data) + a per-sig count; the sizes feed maxtokens() bounds.
    The single place that sees both sides of every call → every repo protected automatically."""
    trunc = is_truncated(finish_reason, out_tok, max_tokens)
    try:
        with _lock:
            _gate_calls_db().execute("INSERT INTO gate_calls (sig,model,out_tok,max_tokens,truncated,ts) VALUES (?,?,?,?,?,?)",
                                (sig, model, int(out_tok or 0), int(max_tokens or 0), int(trunc), time.time()))
            _gate_db().commit()
    except Exception:
        pass
    if trunc:
        _warn_truncated(sig, model, out_tok, max_tokens)
    return trunc


_trunc_warned = {}          # sig -> count already seen this process
_TRUNC_ANNOUNCE = (1, 10, 100, 1000)   # first, then at decade boundaries: enough to show it is not stopping


def _warn_truncated(sig, model, out_tok, max_tokens):
    """One line per class, then only at decade boundaries — carrying the RATE, which is the actionable number.
    It used to print once per truncated call: a class truncating 327 times emitted 327 identical lines, which is
    a warning nobody reads. Every other loud path in spendguard dedups; this one did not."""
    import sys
    n = _trunc_warned.get(sig, 0) + 1
    _trunc_warned[sig] = n
    if n not in _TRUNC_ANNOUNCE:
        return
    rate = ""
    rec = None
    try:
        b = maxtokens(sig)
        if b.get("trunc_rate"):
            rate = " — %.1f%% of this class (%d/%d)" % (b["trunc_rate"] * 100, b["n_truncated"],
                                                        b["n"] + b["n_truncated"])
        rec = b.get("recommend")
    except Exception:
        pass
    fix = ("raise max_tokens to >= %d, or omit it entirely (a cap never controlled cost — you are billed on "
           "tokens GENERATED — and a low one destroys the call)" % rec) if rec else \
          "raise max_tokens, or omit it entirely (a cap never controlled cost, and a low one destroys the call)"
    print("[bulkgate] TRUNCATED %s (%s): output hit max_tokens=%s%s. The result is incomplete, and an incomplete "
          "JSON body reads as 'no findings' rather than 'no answer'. Fix: %s"
          % (sig, model, max_tokens, rate, fix), file=sys.stderr)


_cancel_warned = {}          # model -> count of wall-clock deadline cancels seen this process
_cancel_lock = threading.Lock()   # the fan calls note_deadline_cancel CONCURRENTLY — the count must not lose a cancel


def note_deadline_cancel(model, timeout_s=None):
    """Record + SURFACE a call torn down at its wall-clock deadline MID-GENERATION (the request was cancelled with
    c.close()). This is the INVISIBLE waste behind a 'reasoning burned money for nothing' overrun: because the request
    was cancelled, no usage comes back, so the LOCAL result is out_tok=0 / cost=0 and the ledger records $0 — but the
    provider BILLS whatever it generated before the cut, and for a REASONING model that is reasoning tokens that
    produced NO output. A fan that keeps hitting the deadline (concurrency drives latency past it) thus burns real
    money the per-call ledger cannot see (only a provider-truth reconcile catches it). Counted per model and announced
    at decade boundaries (like a truncation), carrying the actionable fix. Never raises. Returns the running count."""
    import sys
    with _cancel_lock:                       # atomic read-modify-write so a concurrent fan can't lose a cancel
        n = _cancel_warned.get(model, 0) + 1
        _cancel_warned[model] = n
    if n not in _TRUNC_ANNOUNCE:              # print OUTSIDE the lock (never hold it across I/O)
        return n
    print("[bulkgate] DEADLINE-CANCELLED %s x%d: torn down at its %ss wall-clock deadline MID-generation. No usage "
          "returned → the LOCAL ledger records $0, but the provider BILLS what it generated (for a reasoning model, "
          "reasoning tokens that produced NO output — the worst spend there is: paid, invisible, and useless). Fix: "
          "give this class MORE deadline (it is being cut mid-thought), or lower concurrency so latency drops below "
          "the deadline. Reconcile against provider truth to see the real $." % (model, n,
          ("%.0f" % timeout_s) if timeout_s else "?"), file=sys.stderr)
    return n


def deadline_cancels():
    """The per-model count of wall-clock deadline cancels seen this process (the invisible reasoning-cut waste) — a
    read-only snapshot for the observability surfaces (CLI `spendguard dispatch` / MCP spendguard_dispatch_state), so
    the counter is queryable, not just printed. Process-local (resets on restart); {} when none. $0."""
    with _cancel_lock:
        return dict(_cancel_warned)


_unhonored_effort = {}       # "model|requested->chosen" -> count of times an explicit effort pin was NOT honored
_unhonored_lock = threading.Lock()   # a fan resolves effort CONCURRENTLY — the count must not lose an event


def note_unhonored_effort(model, requested, chosen):
    """GUARDRAIL A — record + SURFACE, un-swallowably, that a caller's EXPLICIT effort pin was NOT honored on this
    model: the standard 'minimal' COST pin was remapped to the model's verified floor (gpt-5.x floor='none') AND the
    model STILL reasons at that floor (models.reasons_by_default), so the pin bought NO saving. A control the caller
    set that silently does nothing reads as safe and is not — the worst kind (measured: gpt-5.5 at 'none' → ~4,249 out
    tok / ~$0.13 vs gpt-5-mini honored 'minimal' → 121 tok / $0.0005, the $45 warden overspend). Counted per
    (model, requested→chosen) and announced at decade boundaries, carrying the actionable fix. Never raises. Returns
    the running count.

    Never guesses a value — it only names facts models.py already holds (the floor + reasons_by_default) plus the
    caller's own pin. The real FIX is routing a minimal-cost intent to a model whose 'minimal' IS honored (best-value's
    job); this makes the silent non-honor VISIBLE so it can be routed, capped (guardrail D), or knowingly accepted."""
    import sys
    key = "%s|%s->%s" % (model, requested, chosen)
    with _unhonored_lock:                    # atomic RMW so a concurrent fan can't lose an event
        n = _unhonored_effort.get(key, 0) + 1
        _unhonored_effort[key] = n           # the RECORD is committed HERE — before any I/O — so it survives a bad stderr
    if n not in _TRUNC_ANNOUNCE:             # print OUTSIDE the lock (never hold it across I/O)
        return n
    try:                                     # the count is already recorded; the announce line is best-effort and must
        print("[bulkgate] EFFORT PIN NOT HONORED on %s x%d: effort '%s' requested, but this model's verified floor is "
              "'%s' and it STILL reasons at '%s' — the pin buys NO saving here (measured: gpt-5.x at its floor burns "
              "thousands of reasoning tokens vs a honored 'minimal' ~121 tok; this was the $45 warden overspend). "
              "requested_effort=%s chosen_effort=%s recorded. Fix: route a minimal-cost intent to a model that HONORS "
              "'minimal' (best-value does this), or accept the floor cost — guardrail D's budget_usd cap is the backstop."
              % (model, n, requested, chosen, chosen, requested, chosen), file=sys.stderr)
    except Exception:                        # a closed/broken stderr must not lose the (already-recorded) event
        pass
    return n


def unhonored_efforts():
    """The per-(model, requested→chosen) count of explicit effort pins NOT honored this process (guardrail A's silent
    effort-downgrade surface) — a read-only snapshot for the observability surfaces (CLI `spendguard dispatch` / MCP
    spendguard_dispatch_state), so the counter is queryable, not just printed. Process-local (resets on restart);
    {} when none. $0."""
    with _unhonored_lock:
        return dict(_unhonored_effort)


def _pctl(vals, p):
    """Interpolated percentile of token counts, as an int. None for an empty sample.

    THE ALGORITHM LIVES IN calibrate._quantile — there were three copies of "compute a percentile" in this
    repo and all three disagreed. This one returned 0 on empty input, which is the house invariant inverted:
    an empty sample means we do not KNOW the p99 output length, and 0 is a specific, confident, wrong answer
    that a max_tokens decision would then be sized from."""
    from .calibrate import _quantile
    q = _quantile(vals, p)
    return None if q is None else int(q)


# A REASONING model with NO measurements yet: seed its output estimate with a reasoning-INCLUSIVE floor, not a naive
# visible-answer guess. Reasoning (thinking) tokens bill as OUTPUT, so a per_out sized from the visible answer
# under-counts them badly — measured, a per_out=160 estimate came in ~9x low on gpt-5.5 ($51.88 vs $13.69). Conservative
# (leans over, never the naive under) and replaced by the measured p99 the moment real calls land. Override:
# bulkgate.reasoning_out_estimate config.
REASONING_OUT_ESTIMATE = 4000

# GUARDRAIL E — PER-CALL RUNAWAY BREAKER. A completed call whose out_tok is many times the MEASURED norm for its class
# is a runaway (the gpt-5.5 incident: ~4,249 out tok where the coarse-class norm was ~121). Trip when out_tok exceeds
# RUNAWAY_FACTOR x the measured p99 — measured, never a guessed absolute, so it cannot false-trip a class whose real
# outputs are large. RUNAWAY_MIN_SAMPLES guards against a p99 built from too few calls (an unstable norm must not
# accuse). Both are overridable (env → config → default), consistent with every other bulkgate knob.
RUNAWAY_FACTOR_DEFAULT = 3.0     # out_tok > 3x the class p99 ⇒ a runaway (well beyond normal variance, below noise)
RUNAWAY_MIN_SAMPLES = 20         # need at least this many measured outputs before a p99 is trustworthy enough to accuse


def maxtokens(sig, current_max=None, model=None):
    """Data-driven max_tokens bound for a call-class from its OBSERVED output distribution — turns 'guess' into
    'measure'. Returns {n, p50, p95, p99, max, recommend=p99*1.5, truncations, warn}. warn if current_max < p95
    (TRUNCATION RISK) or >> p99 (cost-estimate inflation → false cap trips). For packed calls, feed per-ITEM out_tok.
    `model` (optional): when a class has NO measurements yet AND the model REASONS (models.reasons_by_default), the
    recommend is seeded with the reasoning-inclusive REASONING_OUT_ESTIMATE instead of None — so a first estimate can
    never come back naive-low for a reasoning model (the measured p95/p99, which already INCLUDE reasoning tokens,
    replace it as soon as real calls land)."""
    with _lock:
        rows = _gate_calls_db().execute("SELECT out_tok,truncated FROM gate_calls WHERE sig=? AND out_tok>0", (sig,)).fetchall()
    # CENSORING: a truncated response was cut AT its cap, so its out_tok measures the CAP, not the work.
    # Including those dragged every percentile down — and the recommendation with it, so the more a class
    # truncated the lower the advice went. A ratchet pointing the wrong way. Percentiles come from COMPLETE
    # outputs only; truncated ones are counted, and set a FLOOR (the work was at least that big).
    outs = [r[0] for r in rows if not r[1]]
    trunc_outs = [r[0] for r in rows if r[1]]
    trunc = sum(1 for r in rows if r[1])
    if not outs:
        _rec = int(max(trunc_outs) * 2) if trunc_outs else None
        _warn = ("every observed output was TRUNCATED — the cap is too low to measure the real size"
                 if trunc_outs else None)
        if _rec is None and model is not None:
            try:
                from . import models as _m
                if _m.reasons_by_default(model):        # a reasoning model with no history → seed reasoning-inclusive,
                    _rec = int(os.getenv("SPENDGUARD_BULKGATE_REASONING_OUT_ESTIMATE")   # env → config → default, so the
                               or config._cfg_get("bulkgate", "reasoning_out_estimate",  # knob is consistent across all
                                                  REASONING_OUT_ESTIMATE) or REASONING_OUT_ESTIMATE)   # three surfaces
                    _warn = ("%s reasons (hidden reasoning tokens bill as OUTPUT) and this class has no measurements "
                             "yet — size the estimate at >= %d output tokens, NOT a small visible-answer figure "
                             "(that under-counts reasoning by ~10x — the measured gpt-5.5 miss)." % (model, _rec))
            except Exception:
                pass
        return {"sig": sig, "n": 0, "n_truncated": trunc, "p90": None,
                "trunc_rate": (trunc / float(len(rows)) if rows else 0.0),
                "recommend": _rec, "truncations": trunc, "warn": _warn}
    p90, p95, p99 = _pctl(outs, 0.90), _pctl(outs, 0.95), _pctl(outs, 0.99)
    warn = None
    if current_max is not None:
        if current_max < p95:
            warn = "max_tokens %d < p95 %d — TRUNCATION RISK" % (current_max, p95)
        elif current_max > p99 * 3:
            warn = "max_tokens %d >> p99 %d — inflates worst-case estimate (false cap trips)" % (current_max, p99)
    # The recommendation can never sit below a cap that ALREADY truncated: that work was demonstrably bigger.
    floor = int(max(trunc_outs) * 2) if trunc_outs else 0
    rate = trunc / float(len(rows)) if rows else 0.0
    if rate > 0 and not warn:
        warn = "%.1f%% of calls TRUNCATED (%d/%d) — raise max_tokens to >= %d, or omit it entirely" % (
            rate * 100, trunc, len(rows), max(int(p99 * 1.5), floor))
    return {"sig": sig, "n": len(outs), "n_truncated": trunc, "trunc_rate": rate,
            "p50": _pctl(outs, 0.50), "p90": p90, "p95": p95, "p99": p99, "max": max(outs),
            "recommend": max(int(p99 * 1.5), floor), "truncations": trunc, "warn": warn}


def model_outputs(model):
    """The output distribution for a MODEL across every call-class, or {} if too little is recorded.

    The rung between "this exact class is measured" and "fall back to the model's published ceiling". Without
    it, a call-class with no history is estimated at the model's output ceiling — 128,000 tokens for opus and
    gpt-5.5 — which over-states a real answer by roughly two orders of magnitude. Measured: an 8-prompt replay
    estimated at $282 against $12 of real spend, and a 26-request job warned at ~$99.86. Conservative in the
    right direction for a BOUND, useless as an EXPECTATION, and estimate-first stops being usable when the two
    are confused. Same censoring as maxtokens(): a truncated response measures its cap, not the work."""
    with _lock:
        rows = _gate_calls_db().execute(
            "SELECT out_tok,truncated FROM gate_calls WHERE model=? AND out_tok>0", (model,)).fetchall()
    outs = [r[0] for r in rows if not r[1]]
    if not outs:
        return {}
    return {"model": model, "n": len(outs), "p50": _pctl(outs, 0.50), "p90": _pctl(outs, 0.90),
            "p99": _pctl(outs, 0.99), "n_classes": len({r[0] for r in _gate_calls_db().execute(
                "SELECT DISTINCT sig FROM gate_calls WHERE model=? AND out_tok>0", (model,)).fetchall()})}


def _runaway_factor():
    """The runaway trip multiple (out_tok > factor x p99), env → config → default — one knob, three surfaces."""
    try:
        return float(os.getenv("SPENDGUARD_BULKGATE_RUNAWAY_FACTOR")
                     or config._cfg_get("bulkgate", "runaway_factor", RUNAWAY_FACTOR_DEFAULT) or RUNAWAY_FACTOR_DEFAULT)
    except Exception:
        return RUNAWAY_FACTOR_DEFAULT


class _RunawayCounter:
    """Process-local per-(model, sig) runaway-trip counts. self IS the container its methods receive (no free-function
    module mutation) — the admission snapshot reads it cross-call, so the breaker is queryable, not just printed.
    Process-local by design (resets on restart); thread-safe (a fan trips concurrently)."""
    def __init__(self):
        self._counts = {}
        self._lock = threading.Lock()

    def bump(self, key):
        """Atomically record one trip for `key`, returning the running count (committed before any caller I/O)."""
        with self._lock:
            n = self._counts.get(key, 0) + 1
            self._counts[key] = n
            return n

    def snapshot(self):
        with self._lock:
            return dict(self._counts)


_RUNAWAYS = _RunawayCounter()


def note_runaway(sig, model, out_tok, norm_p99, basis):
    """GUARDRAIL E — record + SURFACE, un-swallowably, a PER-CALL RUNAWAY: a completed call whose out_tok is many times
    the MEASURED p99 norm for its class. A reasoning model can emit thousands of tokens that bill as output while the
    output ceiling — deliberately loose so reasoning has headroom — never truncates it (the gpt-5.5 incident: ~4,249
    out tok vs a ~121 norm, billed in full x N). The breaker does NOT abort mid-call (a cancelled reasoning call still
    BILLS what it generated and returns nothing — the worst spend there is, see note_deadline_cancel); it TRIPS and
    records so the runaway is VISIBLE, while guardrail D's budget_usd cap bounds the dollars. Counted per (model, sig)
    and announced at decade boundaries, carrying the fix. Never raises. Returns the count. Measured norm only — never a
    guessed absolute — so it cannot accuse a class whose real outputs are large."""
    import sys
    n = _RUNAWAYS.bump("%s|%s" % (model, sig))   # the RECORD is committed HERE — before any I/O — survives a bad stderr
    if n not in _TRUNC_ANNOUNCE:                  # print OUTSIDE the count update (never announce every trip)
        return n
    try:                                          # the count is already recorded; the announce line is best-effort
        print("[bulkgate] RUNAWAY x%d on %s (%s): a call emitted %s output tokens — >%.1fx the measured %s p99 of %s "
              "(reasoning bills as output; the loose ceiling never truncates it). NOT aborted (a cut reasoning call "
              "still bills for nothing); recorded so it is visible. Fix: route this class to a model whose 'minimal' is "
              "honored, or accept it — guardrail D's budget_usd cap bounds the $." % (
              n, model, sig, out_tok, _runaway_factor(), basis, norm_p99), file=sys.stderr)
    except Exception:                             # a closed/broken stderr must not lose the (already-recorded) trip
        pass
    return n


def runaways():
    """The per-(model, sig) count of per-call runaway trips this process (guardrail E — out_tok >> the measured p99) —
    a read-only snapshot for the observability surfaces (CLI `spendguard dispatch` / MCP spendguard_dispatch_state), so
    the breaker is queryable, not just printed. Process-local (resets on restart); {} when none. $0."""
    return _RUNAWAYS.snapshot()


def check_runaway(sig, model, out_tok, norm=None):
    """GUARDRAIL E's COST-ANOMALY TEST — is this completed call's out_tok a statistical OUTLIER vs the class's MEASURED
    token-count distribution? This is ARITHMETIC on billed tokens (tokens = dollars), NOT a semantic judgement of the
    response: it reads NO content and renders NO verdict on the answer's quality — a 4,249-token reply costs ~35x a
    121-token one whether its content is good or bad, and the COST is the fact being monitored. So a measured threshold
    (out_tok > factor x p99) is the right tool, exactly as a p99-latency alert is — not an LLM meaning-call per request.
    Trips (records via note_runaway) only when a TRUSTWORTHY norm exists: the per-class p99 (>= RUNAWAY_MIN_SAMPLES measured outputs),
    else the per-MODEL p99 as a wider fallback (the incident's class was cold but the model was warm). No trustworthy
    norm (a genuinely cold class AND model) ⇒ NO trip — the honest answer is 'cannot judge yet', with guardrail D as the
    $ backstop, never a guessed absolute ceiling that would false-accuse. `norm` may pass a pre-recorded maxtokens(sig)
    dict so the runaway can't inflate its OWN baseline. Returns (tripped, detail). Never raises."""
    try:
        ot = int(out_tok or 0)
        if ot <= 0 or not sig:
            return False, None
        factor = _runaway_factor()
        _mx = norm if isinstance(norm, dict) else (maxtokens(sig) or {})
        p99 = _mx.get("p99")
        n = int(_mx.get("n") or 0)
        basis = "class"
        if not (p99 and n >= RUNAWAY_MIN_SAMPLES):     # class norm not trustworthy → widen to the model's own p99
            _mo = model_outputs(model) or {}
            p99, n, basis = _mo.get("p99"), int(_mo.get("n") or 0), "model"
        if not (p99 and n >= RUNAWAY_MIN_SAMPLES):     # neither is trustworthy → cannot judge (D is the $ backstop)
            return False, None
        if ot > factor * float(p99):
            note_runaway(sig, model, ot, int(p99), basis)
            return True, {"out_tok": ot, "p99": int(p99), "basis": basis, "factor": factor}
        return False, None
    except Exception:
        return False, None                             # telemetry must never break the call


def truncated_recently(sig, window_sec=None):
    """Did this sig TRUNCATE in the recent window? A truncated sample is NOT a passing test — it must not authorize a
    bulk run, so the max_tokens bug is structurally caught by the SAME gate (test_job flips verified→False on it)."""
    cut = time.time() - (window_sec or rt_window_sec())
    with _lock:
        r = _gate_calls_db().execute("SELECT COALESCE(SUM(truncated),0) FROM gate_calls WHERE sig=? AND ts>=?", (sig, cut)).fetchone()
    return bool(r and r[0])


def check_compute(sig, est_usd, hours=None, force=False):
    """REMOTE-COMPUTE (GPU / vast.ai) test-first gate — the same estimate+test rule as check_bulk, on the compute-$
    axis. A big/long launch (est_usd > bulk_min_usd) needs a FRESH estimate + a verified SHORT test run (a small/short
    instance that proved the workload before scaling fleet×duration) — record_tested after that short run. Composes
    with the cap in resources.compute_exceeded. Consumers call this before launching; raises GateBlocked in block mode."""
    if float(est_usd or 0) <= bulk_min_usd():
        return "trivial"
    if gate_status(sig)["fresh"]:
        return "pass"
    forced = bool(force) or os.getenv("GATE_FORCE") == "1"
    m = mode()
    if m == "off":
        return "allow:off"
    tag = "compute(%sh)" % hours if hours else "compute"
    if forced:
        _log_block(sig, tag, int(hours or 0), est_usd, "override")
        return "allow:force"
    if m == "warn":
        _log_block(sig, tag, int(hours or 0), est_usd, "would-block")
        return "allow:warn"
    _log_block(sig, tag, int(hours or 0), est_usd, "blocked")
    raise GateBlocked(
        "BLOCKED compute %s: a ~$%.2f%s launch needs estimate+test FIRST — a SHORT test instance that verified the "
        "workload, then re-run. estimate_job(sig,'compute',worst_case_usd,1) + test_job. Override (logged): GATE_FORCE=1."
        % (sig, float(est_usd or 0), (" over %sh" % hours) if hours else ""))


def estimate_job(sig, model, est_usd, est_count):
    """First-class unblock helper (ships IN spendguard so consumers adopt it, not hand-roll it): record the WORST-CASE
    estimate. = record_estimate; named to read as step 1 of estimate → test → run."""
    return record_estimate(sig, model, est_usd, est_count)


def test_job(sig, run_fn, n=None, verify_fn=None, contract=None, items=None):
    """Step 2 of estimate → test → run: execute a <= preview_max SAMPLE (the gate always allows it — it IS the
    test), CHECK ITS OUTPUT against the declared shape, and record what happened.

        test_job(sig, run_fn, n=5, contract=["patient_id", "findings"], items=pages[:5])

    `contract` is checked against EVERY item of the sample (see output_contract) — the failure that matters is
    the one at item 400, not item 1. `items` are the sample's INPUTS; their fingerprint is stored so a test on
    three toy rows cannot authorize a run over the real corpus.

    NO CONTRACT AND NO verify_fn → the test is recorded UNVERIFIED. It used to be recorded as verified ("None →
    trust that it ran"), which authorized full batches on a sample that proved only that the API returned
    something. The run is still allowed under `warn`/`off` and via GATE_FORCE — but the gate no longer claims a
    verification that never happened."""
    from . import output_contract
    n = min(int(n or preview_max()), preview_max())
    out = run_fn(n)
    res = output_contract.check_items_against_contract(out if contract is not None else [], contract) if contract is not None else None
    if contract is not None:
        ok = res.clean
        if not ok:
            import sys
            print("[bulkgate] test for %s did NOT satisfy the contract: %s" % (sig, res.summary()), file=sys.stderr)
    elif verify_fn is not None:
        ok = bool(verify_fn(out))
    else:
        ok = False
        import sys
        print("[bulkgate] test for %s recorded UNVERIFIED — no contract and no verify_fn, so nothing checked the "
              "output. Pass contract=[...keys] / a schema / a callable to authorize a bulk run." % sig,
              file=sys.stderr)
    if truncated_recently(sig):                          # a SILENTLY-TRUNCATED sample is NOT a passing test —
        ok = False                                       # it must not authorize the bulk run (the max_tokens bug,
        import sys                                        # caught structurally by the same gate)
        print("[bulkgate] test for %s TRUNCATED → recording verified=FALSE. Raise max_tokens "
              "(`spendguard maxtokens %s`) and re-test." % (sig, sig), file=sys.stderr)
    record_tested(sig, n, verified=ok, contract=contract, result=res,
                  data_sig=output_contract.data_signature(items) if items else None)
    return out


# ── EVAL: the lifecycle checkpoint ABOVE the shape-test. test_job asks "did the sample PARSE the declared shape";
#    eval_job asks "is the sample GOOD ENOUGH against a STATED bar". The verdict is a JUDGEMENT, so an LLM decides it
#    (agentic — never a keyword/threshold rubber-stamp, per CLAUDE.md); the gate's later check ("does a passing eval
#    exist?") is the only mechanical part. Mirrors advisor.reconstruct (config.advisor_judge_model, caged as meta). ──
_EVAL_SYS = ("You are a strict, honest evaluator. You are given a STATED BAR (the quality criterion a task's output "
             "must meet) and a SAMPLE of that task's actual outputs from a small test run. Decide whether the SAMPLE "
             "AS A WHOLE meets the bar. Be specific and unforgiving: a sample that only partly meets the bar, or that "
             "matches the shape but not the intent, FAILS. Do not invent leniency the bar does not state.")
_EVAL_SCHEMA = {"type": "object", "additionalProperties": False,
                "properties": {"pass": {"type": "boolean"},
                               "score": {"type": "number"},            # 0..1 — how well the sample meets the bar
                               "rationale": {"type": "string"}},
                "required": ["pass", "score", "rationale"], "nonempty": ["rationale"]}
_EVAL_SAMPLE_CHARS = 12000       # how much of the sample the judge sees; over this, a WHOLE prefix + an HONEST note
_EVAL_OUT_CEILING = 1500         # OUTPUT budget for the verdict — a structured {pass,score,rationale} needs room, and
                                 # a reasoning judge model must not have it eaten by hidden thinking (mirrors advisor._MINE_OUT)


def _eval_verdict_from_result(r):
    """Parse the judge's structured verdict. FAIL-SAFE: an unparseable or empty judge reply is NOT a pass — an eval
    that did not clearly clear the bar cannot authorize scale, so ambiguity blocks rather than waves work through."""
    from . import output_contract
    txt = (r or {}).get("text") or ""
    obj, _ = output_contract._as_obj(txt) if txt else (None, False)
    if not isinstance(obj, dict):
        return {"pass": False, "score": 0.0, "rationale": "judge returned no parseable verdict — treated as FAIL"}
    return {"pass": bool(obj.get("pass")), "score": float(obj.get("score") or 0.0),
            "rationale": str(obj.get("rationale") or "")[:500]}


def eval_job(sig, bar, sample, model=None):
    """Step 2.5 of estimate → test → EVAL → run: an AGENTIC judge scores the TEST SAMPLE against a STATED bar and
    records pass/fail, so a later scale run is authorized only when a fresh PASSING eval exists.

        eval_job(sig, bar="every row cites a real source id and a non-empty finding; no invented codes",
                 sample=test_outputs)

    `bar` is REQUIRED (an eval with no criterion is an empty rubber-stamp — refused). `sample` is the test outputs
    (a list, or already-joined text). The judge is a CHEAP configured model (gate.eval_model, else
    config.advisor_judge_model), run under the meta intent so its own tiny spend is attributed and it never recurses
    into this gate. The verdict is the LLM's — never decided here by keyword. Returns {pass, score, rationale}."""
    bar = (bar or "").strip()
    if not bar:
        raise ValueError("eval_job needs a STATED bar (what a passing output looks like) — pass bar='...'.")
    from . import adapters, calls, config
    from .advisor import META                                    # ONE source of the meta-intent prefix
    judge = model or _gate_setting("eval_model", None, str) or config.advisor_judge_model()
    body = sample if isinstance(sample, str) else "\n---\n".join(str(x) for x in sample)
    shown = body[:_EVAL_SAMPLE_CHARS]
    more = "" if len(body) <= _EVAL_SAMPLE_CHARS else (       # HONEST bound — the judge is told it saw a prefix
        "\n\n[showing the first %d of %d chars of the sample]" % (_EVAL_SAMPLE_CHARS, len(body)))
    prompt = ("STATED BAR (a passing sample must meet this):\n" + bar +
              "\n\nSAMPLE OUTPUTS from the small test run:\n" + shown + more +
              "\n\nDoes the SAMPLE meet the BAR? Return {pass, score 0..1, rationale}.")
    with calls.context(intent=f"{META}:eval"):
        r = adapters.call(judge, prompt, system=_EVAL_SYS, schema=_EVAL_SCHEMA, max_tokens=_EVAL_OUT_CEILING)
    v = _eval_verdict_from_result(r)
    record_eval(sig, bar, v["pass"], score=v.get("score"), note=v.get("rationale"), model=judge)
    return v


_rt_window = {}    # sig -> [recent call timestamps] — in-process burst tracking for the realtime gate
_rt_warned = {}    # sig -> last warn ts — warn-mode log dedup (a big un-adopted loop must not spam one line per call)


def rt_window_sec():
    return _gate_setting("rt_window_sec", 600.0, float)   # rolling window (default 10 min) for "a burst of same-sig calls"


def check_realtime(sig, model, est_usd=0.0, force=False):
    """Realtime BURST gate — a LOOP of realtime calls is the discouraged alternative to the Batch API and must obey the
    same estimate+test-first rule. Track per-sig calls in a rolling in-process window; the first `preview_max` are the
    allowed TEST sample, beyond that the burst needs a FRESH estimate + verified test (delegates to check_bulk on the
    cumulative count/$) or it is blocked/warned. Catches the runaway loop (the 47k-call balloon / the $5.61 escalation).
    Returns the decision; raises GateBlocked in block mode on an untested burst."""
    now = time.time()
    with _lock:
        w = _rt_window.setdefault(sig, [])
        cut = now - rt_window_sec()
        w[:] = [t for t in w if t >= cut]
        w.append(now)
        n = len(w)
    if n <= preview_max():
        return "preview"                                         # still within the allowed test sample
    if mode() == "warn":                                         # warn-mode dedup: log/record the burst ONCE per window
        last = _rt_warned.get(sig, 0)
        if now - last < rt_window_sec():
            return "allow:warn"                                  # already flagged this burst — enforce silently
        _rt_warned[sig] = now
    return check_bulk(sig, model, n, (float(est_usd or 0.0)) * n, force=force)   # cumulative burst est (block mode stops at the cap)


@contextlib.contextmanager
def gated_batch(sig, model):
    """Ordered unblock wrapper so a consumer CAN'T run before estimate+test+eval:
        with bulkgate.gated_batch(sig, model) as job:
            job.note_estimate(worst_case_usd, count)
            sample = job.test(n, run_fn, contract=[...], items=[...])   # <=preview_max sample (allowed), verify shape
            job.eval(bar="what a passing output looks like")            # AGENTIC verdict on that sample (defaults to it)
            job.gated_submit(count, est_usd, submit_fn)      # check_bulk (raises if estimate/test/eval missing) → submit_fn()
    a consumer's batch pool becomes a CONSUMER of this, not a reimplementation."""
    class _Job:
        _contract = None
        _items = None
        _sample = None

        def note_estimate(self, est_usd, count):
            record_estimate(sig, model, est_usd, count)
            return self

        def test(self, n, run_fn, verify_fn=None, contract=None, items=None):
            self._contract = contract                             # remembered so .gated_submit() asserts the SAME shape
            self._items = items
            self._sample = test_job(sig, run_fn, n=n, verify_fn=verify_fn, contract=contract, items=items)
            return self._sample                                   # remembered so .eval() can judge it without re-running

        def eval(self, bar, sample=None, model=None):
            """AGENTIC eval of the test sample against a STATED bar. Defaults to the sample .test() just produced."""
            return eval_job(sig, bar, sample if sample is not None else self._sample, model=model)

        def gated_submit(self, count, est_usd, submit_fn, force=False):
            from . import output_contract
            ds = output_contract.data_signature(self._items) if getattr(self, "_items", None) else None
            check_bulk(sig, model, count, est_usd, force=force,   # raises GateBlocked if estimate/test missing
                       contract=getattr(self, "_contract", None), data_sig=ds)
            return submit_fn()
    yield _Job()

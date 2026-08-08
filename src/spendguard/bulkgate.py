"""TEST-FIRST + ESTIMATE-FIRST enforcement — make it structurally impossible to run a BULK paid LLM job without a
zero-spend ESTIMATE and a verified small-sample TEST. The protocol used to exist only as discipline and got skipped
(a real consumer's opus escalation spent ~$5.61 unestimated + untested, then crashed). This makes the gate BLOCK instead.

How: two flags — `estimated` and `tested` — attach to a CALL-CLASS SIGNATURE (model + template + schema), persist in
sqlite (survive a fresh `python`), and `check_bulk` REFUSES a bulk submit whose sig lacks FRESH flags. The only path to
a full paid run becomes estimate → small test → verify → run. `model` is part of the sig, so testing Haiku never
authorizes Opus/nano; changing the prompt/schema changes the sig → must re-test (no "tested v1, ran v2").

Surface: record_estimate · record_tested · check_bulk (raises GateBlocked) · status · sig · gated_batch (the ordered
unblock wrapper). Rollout via SPENDGUARD_ENFORCE = off | warn | block (default `warn` — log "would-block" — then `block`).
"""
import os
import time
import json
import hashlib
import sqlite3
import threading
import contextlib
from . import config

PREVIEW_MAX_DEFAULT = 25          # a run of <= this many requests is a PREVIEW/TEST — allowed WITHOUT flags (it IS the test)
BULK_MIN_USD_DEFAULT = 0.50       # below this estimated cost, no enforcement (trivial spend)
FRESHNESS_HOURS_DEFAULT = 24      # flags expire — a stale test can't authorize a much-later run on changed data

_lock = threading.RLock()
_conn = None


class GateBlocked(Exception):
    """Raised when a BULK paid run is attempted without a FRESH estimate+test for its call-class signature."""


def _db():
    global _conn
    if _conn is None:
        with _lock:
            if _conn is None:
                c = sqlite3.connect(config.db_path(), timeout=10, check_same_thread=False)
                c.execute("PRAGMA journal_mode=WAL")
                c.execute(
                    "CREATE TABLE IF NOT EXISTS gate_ledger ("
                    " sig TEXT PRIMARY KEY, model TEXT,"
                    " estimated_at REAL, est_usd REAL, est_count INTEGER,"   # worst-case estimate (incl. escalation)
                    " tested_at REAL, test_n INTEGER, verified INTEGER,"     # a verified small-sample run happened
                    " updated_at REAL)")
                # Additive, forward-only. `verified` alone said a test HAPPENED; these say what it PROVED —
                # which contract the output was checked against, on which data, and what the sample did.
                for col, decl in (("contract", "TEXT"), ("contract_hash", "TEXT"), ("data_sig", "TEXT"),
                                  ("test_parsed", "INTEGER"), ("test_salvaged", "INTEGER"),
                                  ("test_failed", "INTEGER"), ("test_failure", "TEXT")):
                    try:
                        c.execute(f"ALTER TABLE gate_ledger ADD COLUMN {col} {decl}")
                    except sqlite3.OperationalError:
                        pass                                  # already present
                c.commit()
                _conn = c
    return _conn


# ── config (env > config.json gate.<name> > default) ──
def _cfg(name, default, cast):
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
    return _cfg("preview_max", PREVIEW_MAX_DEFAULT, int)


def bulk_min_usd():
    return _cfg("bulk_min_usd", BULK_MIN_USD_DEFAULT, float)


def freshness_hours():
    return _cfg("freshness_hours", FRESHNESS_HOURS_DEFAULT, float)


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
        key = (model or "") + "|" + (prompt or "")[:512]
    return hashlib.sha256(key.encode()).hexdigest()[:16]


def _fresh(ts):
    return bool(ts) and (time.time() - float(ts)) <= freshness_hours() * 3600


def record_estimate(sig, model, est_usd, est_count):
    """Record a ZERO-SPEND worst-case estimate for this call-class (sets estimated_at). WORST-CASE incl. any
    escalation path — not the cheap path (the nano-only estimate that hid the $5.61 opus run is the cautionary tale)."""
    now = time.time()
    with _lock:
        _db().execute(
            "INSERT INTO gate_ledger (sig,model,estimated_at,est_usd,est_count,updated_at) VALUES (?,?,?,?,?,?) "
            "ON CONFLICT(sig) DO UPDATE SET model=excluded.model, estimated_at=excluded.estimated_at, "
            "est_usd=excluded.est_usd, est_count=excluded.est_count, updated_at=excluded.updated_at",
            (sig, model, now, float(est_usd), int(est_count), now))
        _db().commit()
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
        _db().execute(
            "INSERT INTO gate_ledger (sig,tested_at,test_n,verified,contract,contract_hash,data_sig,"
            " test_parsed,test_salvaged,test_failed,test_failure,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(sig) DO UPDATE SET tested_at=excluded.tested_at, test_n=excluded.test_n, "
            "verified=excluded.verified, contract=excluded.contract, contract_hash=excluded.contract_hash, "
            "data_sig=excluded.data_sig, test_parsed=excluded.test_parsed, test_salvaged=excluded.test_salvaged, "
            "test_failed=excluded.test_failed, test_failure=excluded.test_failure, updated_at=excluded.updated_at",
            (sig, now, int(test_n), int(bool(verified)), desc, chash, data_sig or "",
             int(r["parsed"]), int(r["salvaged"]), int(r["failed"]), str(r["first_failure"])[:300], now))
        _db().commit()
    return now


def status(sig, contract=None, data_sig=None):
    """{estimated, tested, verified, fresh, contract, …} for this sig — freshness-aware.

    Pass the CURRENT `contract` / `data_sig` and freshness additionally requires that they MATCH what was
    tested. A test proves something about a shape and a data distribution; carrying it over to a different
    shape or a different corpus is the "tested v1, ran v2" hole the sig already closes for the prompt."""
    from . import output_contract
    with _lock:
        r = _db().execute("SELECT model,estimated_at,est_usd,est_count,tested_at,test_n,verified,"
                          "contract,contract_hash,data_sig,test_parsed,test_salvaged,test_failed,test_failure "
                          "FROM gate_ledger WHERE sig=?", (sig,)).fetchone()
    if not r:
        return {"sig": sig, "estimated": False, "tested": False, "verified": False, "fresh": False,
                "contract": "", "contract_match": False, "data_match": False,
                "reason": "never estimated or tested"}
    est_ok, test_ok = _fresh(r[1]), _fresh(r[4])
    want_c = output_contract.contract_hash(contract) if contract is not None else None
    c_match = True if want_c is None else (want_c == (r[8] or ""))
    d_match = True if not data_sig else (str(data_sig) == (r[9] or ""))
    fresh = est_ok and test_ok and bool(r[6]) and c_match and d_match
    reason = ("" if fresh else
              "estimate stale/missing" if not est_ok else
              "test stale/missing" if not test_ok else
              "the sample output did NOT match the declared contract" if not r[6] else
              "the output contract CHANGED since the test" if not c_match else
              "the test ran on DIFFERENT data")
    return {"sig": sig, "model": r[0], "estimated": est_ok, "est_usd": r[2], "est_count": r[3],
            "tested": test_ok, "test_n": r[5], "verified": bool(r[6]), "fresh": fresh,
            "contract": r[7] or "", "contract_match": c_match, "data_match": d_match, "data_sig": r[9] or "",
            "parsed": r[10] or 0, "salvaged": r[11] or 0, "failed": r[12] or 0, "failure": r[13] or "",
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
    print("[bulkgate] %s %s (%s): %d reqs ~$%.2f without fresh estimate+test"
          % (decision.upper(), sig, model, count, float(est_usd or 0)), file=sys.stderr)


def check_bulk(sig, model, count, est_usd, force=False, contract=None, data_sig=None):
    """Call BEFORE a bulk submit. RAISES GateBlocked if this call-class lacks a FRESH estimate+verified-test — UNLESS:
      • it's a PREVIEW (count <= preview_max AND est_usd <= bulk_min_usd) — that IS the allowed test step,
      • mode is `off` (enforcement disabled), or `warn` (logs 'would-block' but allows — the roll-out grace period),
      • force=True or env GATE_FORCE=1 — an explicit, LOGGED human override (never a silent bypass).
    Returns the decision string ('preview'|'pass'|'allow:<mode/force>'); raises only in `block` mode without flags."""
    pm, bm = preview_max(), bulk_min_usd()
    if count <= pm and float(est_usd or 0) <= bm:
        return "preview"                                          # the test step itself — always allowed
    if status(sig, contract=contract, data_sig=data_sig)["fresh"]:
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
    st = status(sig, contract=contract, data_sig=data_sig)
    detail = ""
    if st.get("failed"):
        detail = (" The last sample FAILED the contract: %d/%d items — %s."
                  % (st["failed"], (st.get("test_n") or 0), st.get("failure") or "?"))
    elif st.get("salvaged"):
        detail = (" The last sample only parsed after stripping a fence/preamble (%d items) — fix the prompt or "
                  "widen the contract." % st["salvaged"])
    raise GateBlocked(
        "BLOCKED %s (%s): bulk run of %d (~$%.2f) needs estimate+test FIRST — %s "
        "(estimated=%s tested=%s contract-verified=%s). Run estimate_job(sig, model, worst_case_usd, count), then "
        "a <=%d-item test_job(sig, run_fn, contract=[...], items=[...]), then re-run.%s Override (logged): "
        "GATE_FORCE=1."
        % (sig, model, count, float(est_usd or 0), st.get("reason") or "not authorized",
           st["estimated"], st["tested"], st["verified"], pm, detail))


# ── max_tokens: truncation DETECTION (the API states it — a fact, not a guess) + data-driven bounds (measure the
#    output distribution) — keyed by the SAME call-class sig. The single chokepoint sees both the request's max_tokens
#    and the response usage, so this protects every repo with zero per-repo work. ──
def _calls_db():
    db = _db()
    db.execute("CREATE TABLE IF NOT EXISTS gate_calls "
               "(sig TEXT, model TEXT, out_tok INTEGER, max_tokens INTEGER, truncated INTEGER, ts REAL)")
    db.execute("CREATE INDEX IF NOT EXISTS idx_gatecalls_sig ON gate_calls(sig)")
    db.execute("CREATE INDEX IF NOT EXISTS idx_gatecalls_model ON gate_calls(model)")  # model-level fill obs (calibrate)
    # TIME is the second termination bound, and it had no measurement at all. Same shape as out_tok: record
    # what actually happened per call-class so a deadline can be sized rather than guessed.
    db.execute("CREATE TABLE IF NOT EXISTS gate_latency "
               "(sig TEXT, model TEXT, seconds REAL, hit_deadline INTEGER, ts REAL)")
    db.execute("CREATE INDEX IF NOT EXISTS idx_gatelat_sig ON gate_latency(sig, model)")
    return db


def is_truncated(finish_reason, out_tok=None, max_tokens=None):
    """Did the response get CUT OFF at the cap? The API says so: Anthropic stop_reason=='max_tokens', OpenAI
    finish_reason=='length'. Belt-and-suspenders: out_tok hitting max_tokens exactly. A fact, not a guess."""
    if (finish_reason or "").lower() in ("length", "max_tokens"):
        return True
    return bool(out_tok and max_tokens and int(out_tok) >= int(max_tokens))


def note_latency(sig, model, seconds, hit_deadline=False):
    """Record how LONG one call took, keyed by call-class — the time analogue of note_response's out_tok.

    Without this a deadline is a guess, and a guessed deadline fails the same way a guessed max_tokens does:
    too low and the call dies after you have already paid for the input, too high and you wait three minutes
    to learn a vendor is down. Measured across four vendors on identical prompts, p90 latency ranged 20.6s to
    116.8s — a single global 180s was simultaneously far too generous for one and marginal for another."""
    try:
        with _lock:
            _calls_db().execute(
                "INSERT INTO gate_latency (sig,model,seconds,hit_deadline,ts) VALUES (?,?,?,?,?)",
                (sig, model, float(seconds or 0), int(bool(hit_deadline)), time.time()))
            _db().commit()
    except Exception:
        pass


def latency(sig=None, model=None):
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
    q = "SELECT seconds,hit_deadline FROM gate_latency" + (" WHERE " + " AND ".join(where) if where else "")
    try:
        with _lock:
            rows = _calls_db().execute(q, args).fetchall()
    except Exception:
        return {}
    done = [r[0] for r in rows if not r[1] and r[0] > 0]
    hits = [r[0] for r in rows if r[1]]
    if not done:
        return ({"n": 0, "deadline_hits": len(hits), "hit_rate": 1.0 if hits else 0.0,
                 "floor": max(hits) if hits else None} if hits else {})
    # FLOAT percentiles. _pctl is built for TOKEN COUNTS and returns an int, which silently destroys this
    # measurement twice over: sub-second latencies all become 0, and a p99 of 0 is FALSY — so a caller
    # testing `if d.get("p99")` skips the class rung and quietly falls back to the model-wide number without
    # saying so. Observed as "p50: 0, p90: 7, p99: 10" on a class whose calls really took fractions of a
    # second. Seconds are not tokens; they need the fractional part.
    def _sec(vals, q):
        v = sorted(vals)
        return round(float(v[min(len(v) - 1, int(len(v) * q))]), 3)

    return {"n": len(done), "p50": _sec(done, 0.50), "p90": _sec(done, 0.90), "p99": _sec(done, 0.99),
            "max": max(done), "deadline_hits": len(hits),
            "hit_rate": len(hits) / float(len(rows)) if rows else 0.0,
            "floor": max(hits) if hits else None}


def note_response(sig, model, out_tok, max_tokens=None, finish_reason=None):
    """Record one response's output size + whether it TRUNCATED, keyed by call-class sig. Truncation → loud warning
    (you paid for input + a cut-off output and got corrupt data) + a per-sig count; the sizes feed maxtokens() bounds.
    The single place that sees both sides of every call → every repo protected automatically."""
    trunc = is_truncated(finish_reason, out_tok, max_tokens)
    try:
        with _lock:
            _calls_db().execute("INSERT INTO gate_calls (sig,model,out_tok,max_tokens,truncated,ts) VALUES (?,?,?,?,?,?)",
                                (sig, model, int(out_tok or 0), int(max_tokens or 0), int(trunc), time.time()))
            _db().commit()
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


def _pctl(vals, p):
    if not vals:
        return 0
    v = sorted(vals)
    k = (len(v) - 1) * p
    f = int(k)
    return int(v[f] if f + 1 >= len(v) else v[f] + (v[f + 1] - v[f]) * (k - f))


def maxtokens(sig, current_max=None):
    """Data-driven max_tokens bound for a call-class from its OBSERVED output distribution — turns 'guess' into
    'measure'. Returns {n, p50, p95, p99, max, recommend=p99*1.5, truncations, warn}. warn if current_max < p95
    (TRUNCATION RISK) or >> p99 (cost-estimate inflation → false cap trips). For packed calls, feed per-ITEM out_tok."""
    with _lock:
        rows = _calls_db().execute("SELECT out_tok,truncated FROM gate_calls WHERE sig=? AND out_tok>0", (sig,)).fetchall()
    # CENSORING: a truncated response was cut AT its cap, so its out_tok measures the CAP, not the work.
    # Including those dragged every percentile down — and the recommendation with it, so the more a class
    # truncated the lower the advice went. A ratchet pointing the wrong way. Percentiles come from COMPLETE
    # outputs only; truncated ones are counted, and set a FLOOR (the work was at least that big).
    outs = [r[0] for r in rows if not r[1]]
    trunc_outs = [r[0] for r in rows if r[1]]
    trunc = sum(1 for r in rows if r[1])
    if not outs:
        return {"sig": sig, "n": 0, "n_truncated": trunc, "p90": None,
                "trunc_rate": (trunc / float(len(rows)) if rows else 0.0),
                "recommend": (int(max(trunc_outs) * 2) if trunc_outs else None), "truncations": trunc,
                "warn": ("every observed output was TRUNCATED — the cap is too low to measure the real size"
                         if trunc_outs else None)}
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
        rows = _calls_db().execute(
            "SELECT out_tok,truncated FROM gate_calls WHERE model=? AND out_tok>0", (model,)).fetchall()
    outs = [r[0] for r in rows if not r[1]]
    if not outs:
        return {}
    return {"model": model, "n": len(outs), "p50": _pctl(outs, 0.50), "p90": _pctl(outs, 0.90),
            "p99": _pctl(outs, 0.99), "n_classes": len({r[0] for r in _calls_db().execute(
                "SELECT DISTINCT sig FROM gate_calls WHERE model=? AND out_tok>0", (model,)).fetchall()})}


def truncated_recently(sig, window_sec=None):
    """Did this sig TRUNCATE in the recent window? A truncated sample is NOT a passing test — it must not authorize a
    bulk run, so the max_tokens bug is structurally caught by the SAME gate (test_job flips verified→False on it)."""
    cut = time.time() - (window_sec or rt_window_sec())
    with _lock:
        r = _calls_db().execute("SELECT COALESCE(SUM(truncated),0) FROM gate_calls WHERE sig=? AND ts>=?", (sig, cut)).fetchone()
    return bool(r and r[0])


def check_compute(sig, est_usd, hours=None, force=False):
    """REMOTE-COMPUTE (GPU / vast.ai) test-first gate — the same estimate+test rule as check_bulk, on the compute-$
    axis. A big/long launch (est_usd > bulk_min_usd) needs a FRESH estimate + a verified SHORT test run (a small/short
    instance that proved the workload before scaling fleet×duration) — record_tested after that short run. Composes
    with the cap in resources.compute_exceeded. Consumers call this before launching; raises GateBlocked in block mode."""
    if float(est_usd or 0) <= bulk_min_usd():
        return "trivial"
    if status(sig)["fresh"]:
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
    res = output_contract.check(out if contract is not None else [], contract) if contract is not None else None
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


_rt_window = {}    # sig -> [recent call timestamps] — in-process burst tracking for the realtime gate
_rt_warned = {}    # sig -> last warn ts — warn-mode log dedup (a big un-adopted loop must not spam one line per call)


def rt_window_sec():
    return _cfg("rt_window_sec", 600.0, float)   # rolling window (default 10 min) for "a burst of same-sig calls"


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
    """Ordered unblock wrapper so a consumer CAN'T run before estimate+test:
        with bulkgate.gated_batch(sig, model) as job:
            job.estimate(worst_case_usd, count)     # record_estimate
            job.test(n, run_fn, verify_fn=None)     # runs a <=preview_max sample (allowed), verifies, record_tested
            job.run(count, est_usd, submit_fn)      # check_bulk (raises if estimate/test missing) → submit_fn()
    a consumer's batch pool becomes a CONSUMER of this, not a reimplementation."""
    class _Job:
        _contract = None
        _items = None

        def estimate(self, est_usd, count):
            record_estimate(sig, model, est_usd, count)
            return self

        def test(self, n, run_fn, verify_fn=None, contract=None, items=None):
            self._contract = contract                             # remembered so .run() asserts the SAME shape
            self._items = items
            return test_job(sig, run_fn, n=n, verify_fn=verify_fn, contract=contract, items=items)

        def run(self, count, est_usd, submit_fn, force=False):
            from . import output_contract
            ds = output_contract.data_signature(self._items) if getattr(self, "_items", None) else None
            check_bulk(sig, model, count, est_usd, force=force,   # raises GateBlocked if estimate/test missing
                       contract=getattr(self, "_contract", None), data_sig=ds)
            return submit_fn()
    yield _Job()

"""TEST-FIRST + ESTIMATE-FIRST enforcement — make it structurally impossible to run a BULK paid LLM job without a
zero-spend ESTIMATE and a verified small-sample TEST. The protocol used to exist only as discipline and got skipped
(a warden opus escalation spent ~$5.61 unestimated + untested, then crashed). This makes the gate BLOCK instead.

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


def record_tested(sig, test_n, verified=True):
    """Record that a verified small-sample (<= preview_max) run happened + its output was VERIFIED (sets tested_at)."""
    now = time.time()
    with _lock:
        _db().execute(
            "INSERT INTO gate_ledger (sig,tested_at,test_n,verified,updated_at) VALUES (?,?,?,?,?) "
            "ON CONFLICT(sig) DO UPDATE SET tested_at=excluded.tested_at, test_n=excluded.test_n, "
            "verified=excluded.verified, updated_at=excluded.updated_at",
            (sig, now, int(test_n), int(bool(verified)), now))
        _db().commit()
    return now


def status(sig):
    """{estimated, tested, verified, fresh, ...} for this sig — freshness-aware. Used by check_bulk + the receipt/doctor."""
    with _lock:
        r = _db().execute("SELECT model,estimated_at,est_usd,est_count,tested_at,test_n,verified "
                          "FROM gate_ledger WHERE sig=?", (sig,)).fetchone()
    if not r:
        return {"sig": sig, "estimated": False, "tested": False, "verified": False, "fresh": False}
    est_ok, test_ok = _fresh(r[1]), _fresh(r[4])
    return {"sig": sig, "model": r[0], "estimated": est_ok, "est_usd": r[2], "est_count": r[3],
            "tested": test_ok, "test_n": r[5], "verified": bool(r[6]), "fresh": est_ok and test_ok and bool(r[6])}


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


def check_bulk(sig, model, count, est_usd, force=False):
    """Call BEFORE a bulk submit. RAISES GateBlocked if this call-class lacks a FRESH estimate+verified-test — UNLESS:
      • it's a PREVIEW (count <= preview_max AND est_usd <= bulk_min_usd) — that IS the allowed test step,
      • mode is `off` (enforcement disabled), or `warn` (logs 'would-block' but allows — the roll-out grace period),
      • force=True or env GATE_FORCE=1 — an explicit, LOGGED human override (never a silent bypass).
    Returns the decision string ('preview'|'pass'|'allow:<mode/force>'); raises only in `block` mode without flags."""
    pm, bm = preview_max(), bulk_min_usd()
    if count <= pm and float(est_usd or 0) <= bm:
        return "preview"                                          # the test step itself — always allowed
    if status(sig)["fresh"]:
        return "pass"                                            # fresh estimate + verified test → authorized
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
    st = status(sig)
    raise GateBlocked(
        "BLOCKED %s (%s): bulk run of %d (~$%.2f) needs estimate+test FIRST "
        "(estimated=%s tested=%s verified=%s). Run estimate_job(sig, model, worst_case_usd, count), then a "
        "<=%d-item test_job(), verify, then re-run. Override (logged): GATE_FORCE=1."
        % (sig, model, count, float(est_usd or 0), st["estimated"], st["tested"], st["verified"], pm))


# ── max_tokens: truncation DETECTION (the API states it — a fact, not a guess) + data-driven bounds (measure the
#    output distribution) — keyed by the SAME call-class sig. The single chokepoint sees both the request's max_tokens
#    and the response usage, so this protects every repo with zero per-repo work. ──
def _calls_db():
    db = _db()
    db.execute("CREATE TABLE IF NOT EXISTS gate_calls "
               "(sig TEXT, model TEXT, out_tok INTEGER, max_tokens INTEGER, truncated INTEGER, ts REAL)")
    db.execute("CREATE INDEX IF NOT EXISTS idx_gatecalls_sig ON gate_calls(sig)")
    return db


def is_truncated(finish_reason, out_tok=None, max_tokens=None):
    """Did the response get CUT OFF at the cap? The API says so: Anthropic stop_reason=='max_tokens', OpenAI
    finish_reason=='length'. Belt-and-suspenders: out_tok hitting max_tokens exactly. A fact, not a guess."""
    if (finish_reason or "").lower() in ("length", "max_tokens"):
        return True
    return bool(out_tok and max_tokens and int(out_tok) >= int(max_tokens))


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
        import sys
        print("[bulkgate] TRUNCATED %s (%s): output hit max_tokens=%s (out=%s) → incomplete/corrupt result. "
              "Size it from data: `spendguard maxtokens %s`." % (sig, model, max_tokens, out_tok, sig), file=sys.stderr)
    return trunc


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
    outs = [r[0] for r in rows]
    trunc = sum(r[1] for r in rows)
    if not outs:
        return {"sig": sig, "n": 0, "recommend": None, "truncations": trunc, "warn": None}
    p95, p99 = _pctl(outs, 0.95), _pctl(outs, 0.99)
    warn = None
    if current_max is not None:
        if current_max < p95:
            warn = "max_tokens %d < p95 %d — TRUNCATION RISK" % (current_max, p95)
        elif current_max > p99 * 3:
            warn = "max_tokens %d >> p99 %d — inflates worst-case estimate (false cap trips)" % (current_max, p99)
    return {"sig": sig, "n": len(outs), "p50": _pctl(outs, 0.50), "p95": p95, "p99": p99, "max": max(outs),
            "recommend": int(p99 * 1.5), "truncations": trunc, "warn": warn}


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


def test_job(sig, run_fn, n=None, verify_fn=None):
    """First-class unblock helper: run a <= preview_max SAMPLE (the gate allows it — that IS the test), (optionally)
    auto-verify its output, and record the test. run_fn(n) executes the n-item sample; verify_fn(out)->bool confirms
    the output is correct (None → trust that it ran). Step 2 of estimate → test → run."""
    n = min(int(n or preview_max()), preview_max())
    out = run_fn(n)
    ok = True if verify_fn is None else bool(verify_fn(out))
    if truncated_recently(sig):                          # a SILENTLY-TRUNCATED sample is NOT a passing test —
        ok = False                                       # it must not authorize the bulk run (the max_tokens bug,
        import sys                                        # caught structurally by the same gate)
        print("[bulkgate] test for %s TRUNCATED → recording verified=FALSE. Raise max_tokens "
              "(`spendguard maxtokens %s`) and re-test." % (sig, sig), file=sys.stderr)
    record_tested(sig, n, verified=ok)
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
    warden's batchpool becomes a CONSUMER of this, not a reimplementation."""
    class _Job:
        def estimate(self, est_usd, count):
            record_estimate(sig, model, est_usd, count)
            return self

        def test(self, n, run_fn, verify_fn=None):
            n = min(int(n), preview_max())
            out = run_fn(n)                                       # a <=preview_max sample — check_bulk allows it
            ok = True if verify_fn is None else bool(verify_fn(out))
            record_tested(sig, n, verified=ok)
            return out

        def run(self, count, est_usd, submit_fn, force=False):
            check_bulk(sig, model, count, est_usd, force=force)   # raises GateBlocked if estimate/test missing
            return submit_fn()
    yield _Job()

#!/usr/bin/env python3
"""Reconstruct the spendguard CALL ERROR LOG in detail from the durable vendor_call log.

WHY THIS EXISTS: the queryable ledger (`calls` table) historically recorded only SUCCESSES, so the error history —
the exact evidence needed to prove a reliability fix — was never in the system of record. But `vendor_call._persist`
has been appending EVERY typed outcome (ok AND failure, with http_status / provider_error / attempts / latency /
timestamp) to `vendor_calls.jsonl` the whole time. This reads that durable log and reconstructs the full, structured
error history so the reliability work is driven by REAL data, never fixtures:

  • the true outcome distribution (overall + per provider + per model), and the SUCCESS RATE baseline the fix must move
  • the HTTP-status split (429/529 overload — pace+retry — vs 400/413 rejection — never retry)
  • time clustering of failures (a one-day storm vs a chronic leak reads very differently)
  • spendguard's OWN CODE BUGS (a Python traceback that surfaced AS a call error) separated from genuine provider
    faults — the former are ours to fix, the latter are what retry/pace/batch must survive
  • sampled REAL error bodies per (provider, kind) → representative fixtures for the reliability suite (never rigged)

$0 — a pure read + aggregation of an on-disk log. No LLM call, no provider call, and it NEVER mutates the ledger.
The vendor_call log path is resolved from spendguard.config (never hardcoded), so it works for any install.
"""
import argparse
import collections
import datetime as _dt
import json
import re
import sys
import time


# The vendor_call outcome vocabulary is spendguard's, not this script's — it is read verbatim from the log rows.
# A row with kind == 'ok' is the single success; everything else is a failure carrying its reason.
_OK = "ok"

# KNOWN PYTHON BUILTIN EXCEPTION tokens whose presence at the START of an error string means SPENDGUARD'S OWN CODE
# raised it (a bug in this repo), NOT a provider/SDK fault. This is FIXED-TOKEN detection (a traceback signature),
# not a judgement about meaning: a provider SDK error (RateLimitError, APIConnectionError, "Error code: 429 …") does
# not start with one of these builtin names, so it is never misfiled as our bug. Fixing an item here is a code fix in
# spendguard; a provider fault is what the queue/retry/pace/batch layer must survive.
_OUR_BUG_EXC = ("NameError", "TypeError", "AttributeError", "KeyError", "IndexError", "ValueError",
                "UnboundLocalError", "ImportError", "ModuleNotFoundError", "SyntaxError", "IndentationError",
                "ZeroDivisionError", "RecursionError", "AssertionError", "NotImplementedError", "OSError",
                "FileNotFoundError", "PermissionError", "StopIteration", "RuntimeError")
_OUR_BUG_RE = re.compile(r"^(?:%s)(?::|\s|\()" % "|".join(_OUR_BUG_EXC))


def _log_path():
    """The durable vendor_call log, located via spendguard.config — never a hardcoded path."""
    from spendguard import config
    return config.HOME / "vendor_calls.jsonl"


def _is_our_bug(err):
    """True when the error string is a Python traceback from spendguard's OWN code (a builtin-exception repr at the
    head), i.e. a bug in THIS repo rather than a provider fault. Format/token detection on a fixed set — not a
    meaning judgement (a provider SDK error never begins with one of these builtin names)."""
    return bool(_OUR_BUG_RE.match((err or "").lstrip()))


def _err_signature(err):
    """A stable GROUPING key for an error string: the leading, non-variable part with the variable tail (ids, sizes,
    JSON bodies, quoted names) stripped. FORMAT normalisation for counting 'the same error' — not a meaning call.
    e.g. 'Error code: 429 - {...}' and 'Error code: 429 - {other}' collapse to 'Error code: 429'."""
    e = (err or "").strip()
    if not e:
        return "«no error string»"
    e = re.split(r"[-{(\[]", e, maxsplit=1)[0].strip()   # cut at the first body/paren/brace/bracket
    e = re.sub(r"\d[\d,\.]*", "N", e)                     # collapse any number run to N (sizes, codes, counts)
    return e[:80] or "«empty»"


def _iter_rows(path, since_ts):
    """Yield each JSONL row at or after since_ts. EVERY line that does not reach the caller is accounted for by a
    NAMED counter (never a silent discard — this reconstruction is evidence, and an evidence pipeline that drops a
    row without saying so is the exact defect this whole effort is about):
      malformed_skipped — a line that would not JSON-parse
      undated_skipped   — a row with a missing/non-numeric ts, so it CANNOT be placed in a time window (only when a
                          window is active; with no window every row is kept regardless of ts)
      out_of_window     — a validly-timestamped row before since_ts
    ts in the log is epoch seconds (float)."""
    seen = kept = malformed = undated = out_of_window = 0
    with open(path) as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            seen += 1
            try:
                row = json.loads(line)
            except (ValueError, TypeError):
                malformed += 1
                continue
            ts = row.get("ts")
            if since_ts is not None:
                if not isinstance(ts, (int, float)):
                    undated += 1                 # NAMED gap: cannot be time-placed → excluded from a windowed view,
                    continue                     #            but COUNTED so the reconstruction is honestly incomplete
                if ts < since_ts:
                    out_of_window += 1
                    continue
            kept += 1
            yield row
    _iter_rows.stats = {"lines_seen": seen, "rows_in_window": kept, "malformed_skipped": malformed,
                        "undated_skipped": undated, "out_of_window": out_of_window}


def reconstruct(since_days=None, samples_per_kind=4):
    """Read the durable vendor_call log and return the structured error history. Pure aggregation; $0."""
    path = _log_path()
    if not path.exists():
        raise SystemExit(f"vendor_call log not found at {path} — nothing to reconstruct")
    since_ts = (time.time() - since_days * 86400) if since_days else None

    total = fails = 0
    by_kind = collections.Counter()
    by_provider_total = collections.Counter()
    by_provider_fail = collections.Counter()
    by_provider_kind = collections.Counter()      # (provider, kind) -> n
    by_model_fail = collections.Counter()         # (provider, model) -> n  (failures only)
    http_status = collections.Counter()           # for failures
    fails_by_day = collections.Counter()          # 'YYYY-MM-DD' -> n
    our_bugs = collections.Counter()              # normalised signature -> n  (spendguard code defects)
    err_sig = collections.Counter()               # (provider, kind, signature) -> n
    samples = collections.defaultdict(list)       # (provider, kind) -> [real error bodies]  (representative fixtures)
    latency_by_kind = collections.defaultdict(list)

    for row in _iter_rows(path, since_ts):
        total += 1
        vendor = row.get("vendor") or "?"
        model = row.get("model") or "?"
        kind = row.get("kind") or "?"
        by_kind[kind] += 1
        by_provider_total[vendor] += 1
        if kind == _OK:
            continue
        # a FAILURE from here down
        fails += 1
        by_provider_fail[vendor] += 1
        by_provider_kind[(vendor, kind)] += 1
        by_model_fail[(vendor, model)] += 1
        st = row.get("http_status")
        http_status[st if st is not None else "«null»"] += 1
        ts = row.get("ts")
        if isinstance(ts, (int, float)):
            fails_by_day[_dt.datetime.utcfromtimestamp(ts).strftime("%Y-%m-%d")] += 1
        else:
            fails_by_day["«undated»"] += 1        # a failure with no usable ts is still counted, under a named bucket
        lat = row.get("latency")
        if isinstance(lat, (int, float)):
            latency_by_kind[kind].append(float(lat))
        err = row.get("error") or row.get("provider_error") or ""
        sig = _err_signature(err)
        err_sig[(vendor, kind, sig)] += 1
        if _is_our_bug(err):
            our_bugs[_err_signature(err)] += 1
        bucket = samples[(vendor, kind)]
        if len(bucket) < samples_per_kind and err and err not in bucket:
            bucket.append(err[:300])

    def _pct(n, d):
        return round(100.0 * n / d, 2) if d else 0.0

    def _lat_stats(vals):
        if not vals:
            return None
        vals = sorted(vals)
        n = len(vals)
        return {"n": n, "min": round(vals[0], 2), "p50": round(vals[n // 2], 2),
                "p95": round(vals[min(n - 1, int(n * 0.95))], 2), "max": round(vals[-1], 2)}

    report = {
        "reconstructed_at": _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds"),
        "source": str(path),
        "window": (f"last {since_days}d" if since_days else "all time"),
        "parse": getattr(_iter_rows, "stats", {}),
        "totals": {"calls": total, "ok": total - fails, "failures": fails,
                   "success_rate_pct": _pct(total - fails, total),
                   "failure_rate_pct": _pct(fails, total)},
        "by_outcome_kind": dict(by_kind.most_common()),
        "by_provider": {
            v: {"calls": by_provider_total[v], "failures": by_provider_fail.get(v, 0),
                "success_rate_pct": _pct(by_provider_total[v] - by_provider_fail.get(v, 0), by_provider_total[v])}
            for v, _ in by_provider_total.most_common()},
        "by_provider_kind": {f"{v}/{k}": n for (v, k), n in by_provider_kind.most_common()},
        "top_failing_models": {f"{v}/{m}": n for (v, m), n in by_model_fail.most_common(20)},
        "http_status_of_failures": {str(k): n for k, n in http_status.most_common()},
        "failures_by_day": dict(sorted(fails_by_day.items())),
        "spendguard_code_bugs": dict(our_bugs.most_common()),   # OUR defects surfaced as call errors — fix these
        "top_error_signatures": {f"{v}/{k} :: {s}": n for (v, k, s), n in err_sig.most_common(30)},
        "latency_by_kind": {k: _lat_stats(v) for k, v in latency_by_kind.items()},
        "sample_error_bodies": {f"{v}/{k}": bodies for (v, k), bodies in samples.items()},
    }
    return report


def _print_summary(rep):
    t = rep["totals"]
    print(f"\n=== RECONSTRUCTED ERROR LOG ({rep['window']}) — source {rep['source']} ===")
    print(f"parse: {rep['parse']}")
    print(f"\nBASELINE: {t['calls']:,} calls  |  {t['ok']:,} ok  |  {t['failures']:,} failures  "
          f"|  success rate {t['success_rate_pct']}%  (target: 100%)")
    print("\noutcome kinds:")
    for k, n in rep["by_outcome_kind"].items():
        print(f"  {k:<20} {n:>7,}")
    print("\nper provider (success rate):")
    for v, d in rep["by_provider"].items():
        print(f"  {v:<12} {d['calls']:>7,} calls  {d['failures']:>6,} fail  {d['success_rate_pct']:>6}% ok")
    if rep["spendguard_code_bugs"]:
        print("\n⚠️  SPENDGUARD CODE BUGS surfaced as call errors (OURS to fix, not provider faults):")
        for sig, n in rep["spendguard_code_bugs"].items():
            print(f"  {n:>4}x  {sig}")
    print("\nHTTP status of failures:")
    for st, n in rep["http_status_of_failures"].items():
        print(f"  {st:<8} {n:>6,}")
    print("\nfailures by day:")
    for day, n in rep["failures_by_day"].items():
        print(f"  {day}  {n:>6,}")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--since-days", type=int, default=None,
                    help="only rows within the last N days (default: all time)")
    ap.add_argument("--out", default="scripts/reliability/error_log_reconstructed.json",
                    help="where to write the full JSON report (in-repo, durable)")
    ap.add_argument("--samples", type=int, default=4, help="real error bodies to keep per (provider,kind) as fixtures")
    a = ap.parse_args(argv)
    rep = reconstruct(since_days=a.since_days, samples_per_kind=a.samples)
    _print_summary(rep)
    with open(a.out, "w") as fh:
        json.dump(rep, fh, indent=2)
    print(f"\nfull structured report → {a.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

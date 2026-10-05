"""LIVE validation that the 429-storm fix holds on the REAL metered Anthropic path (where the 8,088 storms were),
through BOTH governed doors onto the one chokepoint:

  DOOR 1 — the EXPLICIT governed FAN (lane_balance.bulk_delegate): paces concurrency via dispatch admission (the
           connection-window + the cross-process rate window). This is the safety floor the real incident rode.
  DOOR 2 — the COMBO engine (storm_submit.submit_storm): the governed entry to the SAME StormCoalescer the IMPLICIT
           4b door (a raw adapters.call fan) routes into — it paces the sustainable realtime share and diverts the
           overflow to the Batch API. Validating the engine here, through a governed entry, proves pace+batch on the
           real API WITHOUT authoring a hand-rolled paid fan (the honestreview `ungoverned_llm_fan` FLOOR, correctly,
           forbids committing that). The IMPLICIT adapters.call→4b seam-routing itself is proven offline
           (tests/test_storm_route_both_doors.py, test_storm_route_explicit_fan_optout.py, test_storm_route_mechanism.py).

Both doors run MODEST vs a representative cap (tiny haiku calls, a one-word reply) so this exercises the real pacing
machinery WITHOUT a real 1000/min storm. Door 2 runs with a HIGH storm horizon so the cohort stays UNDER the realtime
budget and rides realtime — it must NOT divert to a real multi-hour Anthropic Batch API during a quick live check; the
batch-divert leg is proven offline (tests/test_incident_replay_storm.py).

THE SLO (the caller-facing guarantee, per door): ZERO 429s SURFACED to the caller AND every submitted request returns
usable text (N:N). A 429 the governor ABSORBED (re-queued under the ratcheting window) is the mechanism working, not a
failure; it is REPORTED, never counted against the SLO (the same I2 SLO as tests/test_connection_storm_reliability.py).

LEDGER EVIDENCE IS KEYED ON intent + rowid, NOT chain. The thread-local `chain` tag does NOT propagate into a fan's
worker threads (so fanned rows carry chain=NULL), but the INTENT does; a monotonic `rowid` high-water mark captured
BEFORE each door isolates that door's rows. The pass REQUIRES `rows_recorded >= N` so a blind/empty read can never pass
vacuously (the "0 rows / $0 billed" hollow pass this check exists to prevent).

Estimate-FIRST (the API spend protocol): --plan prints the call count + $ estimate and spends $0. --run executes and
reports, per door, surfaced 429s, ledger rows + absorbed 429s, N:N, served_via, and the REAL billed $ (summed from the
door's ledger rows). Spend is bounded at the seam by the per-intent/global running cap (gate guardrail D) on every call.

Usage:  python scripts/probe/live_validate_no_429_storm.py --plan
        python scripts/probe/live_validate_no_429_storm.py --run [--n 40] [--rpm 120] [--workers 20] [--budget 1.0]
                                                                  [--door both|governed|combo] [--model <id>]
"""
import argparse
import os
import sqlite3
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "src"))
import spendguard  # noqa: E402
spendguard.require()
from spendguard import model_catalog, lane_balance, pricing, config, calls  # noqa: E402
from spendguard import content_tokens, expected_output, storm_submit, storm_route  # noqa: E402

INTENT = "live:no-429-storm-validation"
_SYS = "You are terse. One word."
EXPECTED_OUT_TOK = 5   # a one-word ordinal reply — a DECLARED expectation (expected_output.expect basis='declared'),
#                        NOT an invented literal fed to a cost function (the $34→$380 defect this repo guards against)
_HIGH_HORIZON_S = 3600  # door 2: realtime budget = (rpm/60) * horizon; a high horizon keeps the cohort UNDER budget so
#                         it rides realtime and never diverts to a real multi-hour Batch API during a quick live check


def _prompt(i):
    # UNIQUE per call — identical prompts dedupe/cache-serve at $0 and never exercise the metered path (a hollow pass).
    # A distinct nonce forces N genuine metered round-trips.
    return "Reply with exactly one word naming the ordinal: item number %d of the validation run." % i


def _resolve_haiku():
    """A cheap anthropic model id FROM THE CATALOG'S OWN family declaration — `model_family_map["haiku"]`, the catalog
    author's AUTHORITATIVE classification that we READ, not a substring guess at the id's text ("is this a Haiku
    model?" is a meaning question the catalog already answers; `"haiku" in mid` would misread "anthropic-haiku-compat-
    proxy"). Returns the first declared id that still has a record, else None — the caller fails honestly / pins --model."""
    rec = model_catalog.vendor_record("anthropic") or {}
    for mid in ((rec.get("model_family_map") or {}).get("haiku") or []):
        if model_catalog.model_record(mid):
            return mid
    return None


def _ledger_db():
    return config.db_path() if hasattr(config, "db_path") else os.path.expanduser("~/.spendguard/spend.db")


def _max_rowid():
    """The ledger's current max rowid — a monotonic high-water mark captured BEFORE each door so the door's own rows
    read back by `rowid > mark` (+ this run's INTENT), WITHOUT relying on the chain tag (chain does not propagate into
    a fan's worker threads; intent does). None on a read failure."""
    try:
        con = sqlite3.connect(_ledger_db())
        rid = con.execute("SELECT COALESCE(MAX(rowid),0) FROM calls").fetchone()[0]
        con.close()
        return int(rid)
    except Exception:
        return None


def _door_ledger(rowid_before):
    """(rows recorded, http_status=429 count, billed $) for THIS door — the calls.py rows with this run's INTENT that
    landed after `rowid_before`. (None, None, None) on a read failure — surfaced honestly, never a false zero."""
    if rowid_before is None:
        return None, None, None
    try:
        con = sqlite3.connect(_ledger_db())
        where, args = "rowid > ? AND intent = ?", (rowid_before, INTENT)
        rows = con.execute("SELECT COUNT(*) FROM calls WHERE " + where, args).fetchone()[0]
        n429 = con.execute("SELECT COUNT(*) FROM calls WHERE " + where + " AND http_status=429", args).fetchone()[0]
        cost = con.execute("SELECT COALESCE(SUM(cost),0) FROM calls WHERE " + where, args).fetchone()[0]
        con.close()
        return rows, n429, float(cost or 0.0)
    except Exception:
        return None, None, None


def _per_call_estimate(model):
    """$/call from a MEASURED input count (real system+prompt text, provider-aware tokenizer) and the SANCTIONED output
    expectation (expected_output.expect, basis-tracked) — never integer literals fed to a cost function."""
    try:
        in_tok = content_tokens.count_tokens(_SYS + "\n" + _prompt(0), provider="anthropic", model=model)
        out_tok, _basis = expected_output.expect(model, sig=INTENT, declared=EXPECTED_OUT_TOK)
        return pricing.realtime_cost(model, in_tok, out_tok) or 0.0
    except Exception:
        return 0.0


def _run_governed_door(model, n, workers, budget_usd):
    """DOOR 1 — the explicit governed fan. bulk_delegate paces concurrency through dispatch admission."""
    tasks = [{"i": k} for k in range(n)]
    with calls.context(intent=INTENT):   # intent propagates into the worker threads; chain would not, so we don't rely on it
        return lane_balance.bulk_delegate(
            tasks, intent=INTENT, system=_SYS, prompt_for=lambda t: _prompt(t["i"]),
            model_for=lambda t: "anthropic:%s" % model, metered_only=True,
            max_workers=workers, deadline_s=300.0, budget_usd=budget_usd, reasoning="minimal",
            force=True)   # own the lane-bulk gate ($-tiny validation) so the fan actually EXECUTES + records


def _run_combo_door(model, n, workers):
    """DOOR 2 — the COMBO engine via submit_storm (the governed entry to the StormCoalescer). Paces the realtime share
    and would divert overflow to the Batch API; the high horizon keeps this cohort all-realtime for a quick check. The
    coalescer's pools live inside the library's governed engine — this is NOT a hand-rolled paid fan in the probe."""
    tasks = list(range(n))
    with calls.context(intent=INTENT):
        return storm_submit.submit_storm(
            tasks, intent=INTENT, model="anthropic:%s" % model, system=_SYS, reasoning="minimal",
            prompt_for=_prompt, deadline_s=_HIGH_HORIZON_S, max_workers=workers, collect_timeout_s=300.0)


def _assess(label, res, n, elapsed, rpm, rows, n429, billed):
    """Report one door and return passed. SLO = 0 SURFACED 429s + N:N served + REAL metered rows recorded (>= N)."""
    res = res or []
    surfaced = sum(1 for r in res if isinstance(r, dict) and (r.get("status_code") or r.get("http_status")) in (429, 529))
    ok_rows = sum(1 for r in res if isinstance(r, dict) and r.get("text") and not r.get("error"))
    fails = [r for r in res if not (isinstance(r, dict) and r.get("text") and not r.get("error"))]
    served_via = {}
    for r in res:
        key = (r.get("served_via") or "direct") if isinstance(r, dict) else "non-dict"
        served_via[key] = served_via.get(key, 0) + 1
    observed_rpm = (len(res) / elapsed * 60.0) if elapsed > 0 else float("inf")
    # REAL metered round-trips must be RECORDED (>= N), else the ledger evidence is blind and the pass is hollow.
    recorded_ok = (rows is not None) and (rows >= n)
    passed = (surfaced == 0) and (ok_rows == n) and recorded_ok
    print("\n  == DOOR: %s ==" % label)
    print("    surfaced 429s to caller: %d    N:N served: %d/%d    served_via: %s" % (surfaced, ok_rows, n, served_via))
    print("    ledger (intent+rowid): %s rows recorded (need >= %d), %s absorbed 429(s) %s, billed $%s" % (
        rows, n, n429,
        "(absorbed — 0 surfaced; the governor re-queued them)" if (n429 or 0) > 0 and surfaced == 0 else "",
        ("%.6f" % billed) if billed is not None else "?"))
    print("    elapsed: %.1fs  observed ~%.0f req/min vs cap %d rpm" % (elapsed, observed_rpm, rpm))
    if fails:   # NEVER hide failures — surface each for inspection
        print("    FAILURES (%d) — NOT a clean run:" % len(fails))
        for r in fails[:10]:
            print("      - %s" % ((r or {}).get("error") or "no text / unknown" if isinstance(r, dict) else repr(r)))
    if passed:
        print("    → PASS (0 surfaced 429s, all %d served, %s real metered rows recorded)" % (n, rows))
    elif not recorded_ok:
        print("    → REVIEW: only %s metered rows recorded for N=%d — the calls did NOT demonstrably hit the metered "
              "path (hollow: cache/lane/mis-tag); ledger read %s" % (rows, n, "ok" if rows is not None else "FAILED"))
    else:
        print("    → REVIEW: see surfaced 429s / failures above")
    return passed


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--plan", action="store_true")
    ap.add_argument("--run", action="store_true")
    ap.add_argument("--n", type=int, default=40)
    ap.add_argument("--rpm", type=int, default=120)
    ap.add_argument("--workers", type=int, default=20)
    ap.add_argument("--budget", type=float, default=1.0)
    ap.add_argument("--door", choices=("both", "governed", "combo"), default="both")
    ap.add_argument("--model", default=None, help="pin the anthropic model id; else resolved from the catalog haiku family")
    a = ap.parse_args()
    if not (a.plan or a.run):
        ap.error("pass --plan or --run")
    model = a.model or _resolve_haiku()
    if not model:
        print("could not resolve an anthropic haiku model from the catalog family map — pin one with --model"); sys.exit(2)

    doors = ["governed", "combo"] if a.door == "both" else [a.door]
    per = _per_call_estimate(model)
    calls_total = a.n * len(doors)
    print("== LIVE 429-storm validation — doors: %s ==" % ", ".join(doors))
    print("  model=%s  N=%d/door  workers=%d  rpm cap=%d  budget=$%.2f/door" % (model, a.n, a.workers, a.rpm, a.budget))
    print("  ESTIMATE: ~$%.6f/call × %d calls (%d door(s)) = ~$%.4f metered (anthropic)  [paced ETA ~%.0fs/door at %d rpm]"
          % (per, calls_total, len(doors), per * calls_total, (a.n / max(1, a.rpm)) * 60.0, a.rpm))
    if a.plan:
        print("  --plan: $0 spent. Re-run with --run."); return

    os.environ["SPENDGUARD_DISPATCH_RPM_ANTHROPIC"] = str(a.rpm)   # pace against a representative cap on the real path
    os.environ["SPENDGUARD_DISPATCH_MANAGE_ALL"] = "1"
    os.environ["SPENDGUARD_STORM_HORIZON_S"] = str(_HIGH_HORIZON_S)  # keep door 2 under the realtime budget (no batch divert)
    results, overall_billed = [], 0.0
    try:
        for door in doors:
            label = "governed (bulk_delegate)" if door == "governed" else "combo (submit_storm → StormCoalescer)"
            rid0 = _max_rowid()           # high-water mark BEFORE this door — isolates its rows by rowid + INTENT
            t0 = time.time()
            res = (_run_governed_door(model, a.n, a.workers, a.budget) if door == "governed"
                   else _run_combo_door(model, a.n, a.workers))
            elapsed = time.time() - t0
            rows, n429, billed = _door_ledger(rid0)
            results.append(_assess(label, res, a.n, elapsed, a.rpm, rows, n429, billed))
            overall_billed += (billed or 0.0)
    finally:
        storm_route.reset_registry()   # close any coalescer created (reap its planner thread)

    all_pass = all(results) and len(results) == len(doors)
    print("\n== OVERALL: %s — %d/%d door(s) clean on the real metered path  ::  total billed $%.6f (est ~$%.4f) =="
          % ("PASS" if all_pass else "REVIEW", sum(1 for p in results if p), len(doors), overall_billed, per * calls_total))
    sys.exit(0 if all_pass else 1)


if __name__ == "__main__":
    main()

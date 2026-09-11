"""Reasoning-STRESS matrix — prove every keyed provider survives the exact trigger that wedged the queue:
a LONG, HIGH-reasoning generation, translated to each provider's own reasoning dialect, bounded at wall-clock.

The existing `reliability.sweep` proves REACHABILITY with a deliberately tiny `_probe=True` ping — so it would
NOT reproduce the kimi-k3 / glm hang (the ping never reasons through a big budget). This harness does the opposite:
it sends a real multi-step reasoning task at reasoning="high" to each provider the install depends on
(targets come from `reliability.plan()`, never hardcoded), and records, per (provider, model), whether the call:
  · ANSWERED   — real text within the wall-clock bound (reasoning param TRANSLATED + accepted + served), or
  · DEADLINE   — cleanly bounded by OUR wall-clock worker (error_type _CallDeadline, client closed → billing stops), or
  · ERROR/RAISED — anything else, recorded VERBATIM (a request-validation 4xx here = a translate bug to see, not hide).

Then two direct proofs of the fix the other conversations needed:
  · BOUND-PROOF — the same call at a deliberately tight timeout MUST return in ~timeout_s (answer or clean deadline),
    NEVER hang. This is the kimi-k3 / glm wedge, exercised against the REAL provider.
  · BEST-VALUE  — one reasoning="best-value" call: spendguard agentically picks model+effort and serves a real answer.

Outcomes are classified by STRUCTURE only (error present? error_type identity? latency vs the bound?) — no
meaning-judgement, no keyword verdict. The final numeric answer is checked against deterministic ground truth as a
FREE correctness signal, but PASS is structural: answered-or-cleanly-bounded within timeout, zero hangs, zero rejects.

DURABLE + RESUMABLE + PER-UNIT ISOLATED (the repo's chunk/never-single-shot doctrine):
  · each row (a REAL paid call) is appended+fsync'd to a JSONL checkpoint the instant it returns — a crash mid-matrix
    never loses paid results, and every paid fact is ALSO independently in the gate ledger (record_call), so the
    checkpoint is a second copy, not the sole one;
  · a rerun with the same --out SKIPS any (phase, spec) already recorded — a failed/interrupted run resumes and
    never re-pays for a unit it already bought;
  · one provider raising does NOT abort the matrix — it is recorded as `raised` and the sweep continues; a DELIBERATE
    gate refusal (SpendGateRefused) is the one exception that propagates (fail-closed, never swallowed).
ESTIMATE-FIRST: default is a zero-spend estimate; `--run` executes under the gate and refuses if the estimate
exceeds `--budget`. Finishes with a forensic receipt: real $ split per provider (all API here).
"""
import argparse
import json
import os
import re
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "src"))

import spendguard                                   # noqa: E402
spendguard.require()                                # fail closed — refuse to run if this interpreter is not gated
from spendguard import reliability, adapters, pricing, gate   # noqa: E402

# ── the reasoning fixture: a multi-step word problem with ONE deterministic answer (test data, computed not asserted) ──
_MONDAY_SOLD_FRACTION = (1, 4)          # sold 1/4 on Monday
_TUESDAY_SOLD_FRACTION = (40, 100)      # sold 40% of the REMAINDER on Tuesday
_AFTER_TUESDAY = 90                     # 90 left after Tuesday
_SHIPMENT_MULTIPLIER = 2                # a shipment then doubled the stock

PROMPT = (
    "A bookstore started the week with some books. On Monday it sold one quarter of them. "
    "On Tuesday it sold forty percent of what remained, which left exactly 90 books. "
    "A shipment then doubled the stock. Work through it step by step, then put the final count "
    "on the last line in the exact form:  ANSWER: <number>"
)


def _expected_answer():
    """Ground truth, computed from the premises (not a bald literal): after-Tuesday count, then doubled = 90×2 = 180."""
    return int(_AFTER_TUESDAY * _SHIPMENT_MULTIPLIER)


REASONING = "high"          # the exact trigger: force each provider's heaviest reasoning path
MAIN_TIMEOUT_S = 60         # generous wall-clock for a real high-reasoning generation
BOUND_TIMEOUT_S = 4         # deliberately tight → must prove OUR bound fires on the real provider (no hang)
MAIN_MAX_OUT = 6000         # room for reasoning + answer in ONE shot (so we test the real path, not the grow-loop)
EST_OUT = 6000              # estimate output generously so we never under-quote
EST_IN = 90                 # the prompt is ~90 tokens
HANG_MARGIN_S = 8           # a return within timeout_s + this is "bounded"; beyond it is a hang


def targets():
    """(provider, model) for every keyed provider — from reliability.plan(), i.e. the ids THIS install depends on
    (kimi-k3 for moonshot, glm for zai, …). Never a hardcoded model list."""
    return reliability.plan()["metered"]


def _price(spec, i, o):
    try:
        return pricing.realtime_cost(spec, i, o)
    except Exception:
        return None


def estimate(tgts=None):
    """Zero-spend $ estimate: main pass (1 high-reasoning call/provider) + bound-proof (1/provider) + best-value
    (~1 workload call). Priced via pricing.realtime_cost at a generous output size. Spends nothing."""
    tgts = tgts if tgts is not None else targets()
    rows, total = [], 0.0
    for prov, mid in tgts:
        c_main = _price(f"{prov}:{mid}", EST_IN, EST_OUT)
        c_bound = _price(f"{prov}:{mid}", EST_IN, 200)          # bound-proof usually deadlines with little/no output
        rows.append({"provider": prov, "model": mid, "main": c_main, "bound": c_bound})
        total += (c_main or 0.0) + (c_bound or 0.0)
    bv = _price(f"{tgts[0][0]}:{tgts[0][1]}", EST_IN, EST_OUT) if tgts else 0.0   # best-value workload
    total += (bv or 0.0)
    return {"rows": rows, "best_value_workload": bv, "total": round(total, 4), "n": len(tgts)}


def _outcome(r):
    """Classify by STRUCTURE only — no meaning-judgement. `error_type` is the exception CLASS name (a structured
    identity token, like an HTTP status), so a clean wall-clock timeout is our own `_CallDeadline`, matched by
    IDENTITY — never by scanning the human-readable message for words like "deadline"."""
    err = r.get("error")
    if not err:
        return "answered", None
    et = r.get("error_type") or ""
    if et == "_CallDeadline":
        return "deadline", None
    if et == "raised":
        return "raised", str(err)[:200]
    return "error", f"{et}: {err}"[:200]          # verbatim — a translate/validation failure is shown, not hidden


def _correct(text):
    """FREE correctness signal (NOT the PASS criterion): parse the number from the fixed-format ANSWER line this
    prompt REQUIRES ("ANSWER: <n>") and compare it to the computed ground truth by EXACT INTEGER EQUALITY. This is a
    deterministic parse of a token whose format I imposed, plus arithmetic — never a semantic judgement of the reply
    (so "ANSWER: 1180" is 1180 != 180 → False, not a substring-of-180 false positive). None when no ANSWER token is
    present → unjudgeable, not "wrong"."""
    if not text:
        return None
    m = re.search(r"ANSWER:\s*\*{0,2}\$?\s*([0-9][0-9,]*)", text, re.IGNORECASE)
    if not m:
        return None
    return int(m.group(1).replace(",", "")) == _expected_answer()


def _safe_call(spec, timeout_s, sig, **extra):
    """One provider call, PER-UNIT ISOLATED: a transport/provider exception becomes a recorded `raised` row so the
    matrix continues; a DELIBERATE gate refusal (SpendGateRefused and every subclass) PROPAGATES — fail-closed."""
    try:
        return adapters.call(spec, PROMPT, reasoning=extra.pop("reasoning", REASONING), max_tokens=MAIN_MAX_OUT,
                             timeout_s=timeout_s, sig=sig, **extra)
    except gate.SpendGateRefused:
        raise                                     # never swallow the gate saying no
    except Exception as e:                        # noqa: BLE001 — any provider/transport failure is DATA, not a crash
        return {"error": f"{type(e).__name__}: {e}", "error_type": "raised", "cost": 0.0, "text": None}


def _sink(path, obj):
    """Durable append-only checkpoint: write one row and fsync, so each paid result survives a later crash. The same
    paid fact (cost/tokens/model) is ALSO written to the gate ledger by adapters.call — this is a second copy."""
    with open(path, "a") as f:
        f.write(json.dumps(obj, default=str) + "\n")
        f.flush()
        os.fsync(f.fileno())


def _done(path):
    """(phase, spec) pairs already SUCCESSFULLY recorded — a rerun skips those (never re-pays) but RETRIES failures.
    Only answered/deadline outcomes count as done; an error/raised unit is retried (a failed call cost ~$0)."""
    seen, prior = set(), []
    if os.path.exists(path):
        with open(path) as f:
            for line in f:
                try:
                    o = json.loads(line)
                except Exception:
                    continue
                prior.append(o)
                if o.get("outcome") not in ("answered", "deadline"):
                    continue                              # a failed unit is NOT done — retry it on resume
                if o.get("phase") in ("main", "bound"):
                    seen.add((o["phase"], o.get("spec")))
                elif o.get("phase") == "best_value":
                    seen.add(("best_value", None))
    return seen, prior


def _badge(label):
    return {"answered": "🟢 ANSWERED", "deadline": "🟡 DEADLINE ", "error": "🔴 ERROR   ",
            "raised": "🔴 RAISED  "}.get(label, label)


def run_matrix(tgts, out_path):
    done, prior = _done(out_path)
    results = {"main": [], "bound": [], "best_value": None, "checkpoint": out_path}
    for o in prior:                                   # seed ONLY the successful prior rows (failed ones are retried below)
        if o.get("phase") == "main" and ("main", o.get("spec")) in done:
            results["main"].append(o)
        elif o.get("phase") == "bound" and ("bound", o.get("spec")) in done:
            results["bound"].append(o)
        elif o.get("phase") == "best_value" and ("best_value", None) in done:
            results["best_value"] = o
    if not prior:
        _sink(out_path, {"phase": "meta", "expected_answer": _expected_answer(),
                         "targets": [f"{p}:{m}" for p, m in tgts], "reasoning": REASONING,
                         "main_timeout_s": MAIN_TIMEOUT_S, "bound_timeout_s": BOUND_TIMEOUT_S})

    print(f"\n== MAIN PASS — reasoning={REASONING!r}, wall-clock {MAIN_TIMEOUT_S}s, max_out {MAIN_MAX_OUT} ==")
    for prov, mid in tgts:
        spec = f"{prov}:{mid}"
        if ("main", spec) in done:
            print(f"  {spec:34} ⏩ resumed (already recorded)")
            continue
        t0 = time.time()
        r = _safe_call(spec, MAIN_TIMEOUT_S, "spendguard:reasoning-stress-main")
        dt = round(time.time() - t0, 1)
        label, detail = _outcome(r)
        row = {"phase": "main", "spec": spec, "outcome": label, "latency_s": dt, "cost": r.get("cost") or 0.0,
               "in_tok": r.get("in_tok"), "out_tok": r.get("out_tok"), "executor": r.get("executor"),
               "correct": _correct(r.get("text")), "detail": detail, "no_hang": dt <= MAIN_TIMEOUT_S + HANG_MARGIN_S}
        results["main"].append(row)
        _sink(out_path, row)                          # durable the instant the paid call returns
        print(f"  {spec:34} {_badge(label)} {dt:5}s  out={row['out_tok']}  ${row['cost']:.5f}"
              f"  correct={row['correct']}  exec={row['executor']}" + (f"  «{detail}»" if detail else ""))

    print(f"\n== BOUND-PROOF — same call at a tight {BOUND_TIMEOUT_S}s wall-clock: must NOT hang ==")
    for prov, mid in tgts:
        spec = f"{prov}:{mid}"
        if ("bound", spec) in done:
            print(f"  {spec:34} ⏩ resumed (already recorded)")
            continue
        t0 = time.time()
        r = _safe_call(spec, BOUND_TIMEOUT_S, "spendguard:reasoning-stress-bound")
        dt = round(time.time() - t0, 1)
        label, detail = _outcome(r)
        bounded = dt <= BOUND_TIMEOUT_S + HANG_MARGIN_S    # returned near the bound (answer OR deadline) — never hung
        row = {"phase": "bound", "spec": spec, "outcome": label, "latency_s": dt, "cost": r.get("cost") or 0.0,
               "bounded": bounded, "detail": detail}
        results["bound"].append(row)
        _sink(out_path, row)
        print(f"  {spec:34} {_badge(label)} {dt:5}s  bounded={bounded}  ${row['cost']:.5f}"
              + (f"  «{detail}»" if detail else ""))

    if ("best_value", None) not in done:
        print("\n== BEST-VALUE — one reasoning='best-value' call (agentic model+effort pick, then serve) ==")
        t0 = time.time()
        rb = _safe_call(f"{tgts[0][0]}:{tgts[0][1]}", MAIN_TIMEOUT_S, "spendguard:reasoning-stress-bestvalue",
                        reasoning="best-value", intent="spendguard:reasoning-stress")
        dt = round(time.time() - t0, 1)
        label, detail = _outcome(rb)
        bv = {"phase": "best_value", "requested": f"{tgts[0][0]}:{tgts[0][1]}",
              "served_model": rb.get("model"), "provider": rb.get("provider"),
              "substituted_from": rb.get("substituted_from"), "best_value": rb.get("best_value"),
              "outcome": label, "latency_s": dt, "cost": rb.get("cost") or 0.0,
              "correct": _correct(rb.get("text")), "detail": detail}
        results["best_value"] = bv
        _sink(out_path, bv)
        print(f"  requested {tgts[0][0]}:{tgts[0][1]} → served {rb.get('provider')}:{rb.get('model')} "
              f"{_badge(label)} {dt}s  ${bv['cost']:.5f}  correct={bv['correct']}"
              + (f"  «{detail}»" if detail else ""))
    return results


def _verdict(results):
    """Structural PASS criteria, tied to the exact challenges. Returns (ok, lines)."""
    lines = []
    main, bound, bv = results["main"], results["bound"], results["best_value"]

    hangs = [r["spec"] for r in main + bound if not (r.get("no_hang") or r.get("bounded"))]
    lines.append((not hangs, "NO HANG — every call returned within its wall-clock bound"
                             + ("" if not hangs else f"  (HUNG: {hangs})")))

    rejects = [f"{r['spec']} {r['detail']}" for r in main if r["outcome"] in ("error", "raised")]
    lines.append((not rejects, "reasoning='high' TRANSLATED + accepted by every provider (no request-rejection/raise)"
                              + ("" if not rejects else f"  ({rejects})")))

    deadlines_clean = all(r["outcome"] not in ("error", "raised") for r in bound)
    lines.append((deadlines_clean, "BOUND-PROOF — the tight-timeout wedge test bounded cleanly on every provider"))

    served = [r for r in main if r["outcome"] == "answered"]
    lines.append((len(served) >= 1, f"AT LEAST ONE provider fully served the high-reasoning task ({len(served)}/{len(main)})"))

    bv_ok = bool(bv and bv["outcome"] in ("answered", "deadline") and bv.get("served_model"))
    lines.append((bv_ok, f"BEST-VALUE resolved a model ({bv and bv.get('served_model')}) and ran without hang/reject"))

    return all(c for c, _ in lines), lines


def _receipt(results):
    """Forensic receipt — REAL $ out the door, split per provider (all API here). est-value n/a (nothing plan-covered)."""
    by_prov = {}
    for r in results["main"] + results["bound"]:
        prov = r["spec"].split(":", 1)[0]
        by_prov[prov] = round(by_prov.get(prov, 0.0) + (r["cost"] or 0.0), 6)
    if results["best_value"]:
        p = results["best_value"].get("provider") or "best-value"
        by_prov[p] = round(by_prov.get(p, 0.0) + (results["best_value"]["cost"] or 0.0), 6)
    total = round(sum(by_prov.values()), 6)
    parts = " + ".join(f"${v:.5f} {k}" for k, v in sorted(by_prov.items(), key=lambda kv: -kv[1])) or "$0"
    print("\n── RECEIPT ──")
    print(f"  Total ${total:.5f} = {parts} API   ::   est-value n/a (all metered, nothing plan-covered)")
    return total


def main(argv=None):
    ap = argparse.ArgumentParser(description="Reasoning-stress matrix across every keyed provider.")
    ap.add_argument("--run", action="store_true", help="actually spend (default: zero-spend estimate)")
    ap.add_argument("--budget", type=float, default=10.0, help="refuse to run if the estimate exceeds this ($)")
    ap.add_argument("--out", default=None, help="durable JSONL checkpoint path (default: alongside this script); "
                                                "reuse it to RESUME without re-paying")
    ap.add_argument("--json", action="store_true", help="also emit machine-readable results at the end")
    args = ap.parse_args(list(argv) if argv is not None else None)

    tgts = targets()
    est = estimate(tgts)
    print(f"Reasoning-stress matrix — {est['n']} keyed providers.  Ground-truth answer = {_expected_answer()}.")
    print(f"Estimate (generous, out≈{EST_OUT}): ~${est['total']:.4f} total")
    for row in est["rows"]:
        print(f"  {row['provider']:10} {row['model']:28} main~${(row['main'] or 0):.5f}  bound~${(row['bound'] or 0):.5f}")
    print(f"  best-value workload ~${(est['best_value_workload'] or 0):.5f}")

    if not args.run:
        print(f"\n  ESTIMATE ONLY (${est['total']:.4f}) — re-run with --run to execute under the gate.")
        return 0

    if est["total"] > args.budget:
        print(f"\n  🔴 REFUSED — estimate ${est['total']:.4f} exceeds --budget ${args.budget:.2f}. Raise the budget to proceed.")
        return 2

    out_path = args.out or os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                        "reasoning_stress_" + time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()) + ".jsonl")
    print(f"\n  ✅ estimate ${est['total']:.4f} ≤ budget ${args.budget:.2f} — executing under the gate.")
    print(f"  durable checkpoint → {out_path}")
    results = run_matrix(tgts, out_path)
    ok, lines = _verdict(results)
    print("\n== VERDICT (challenges from the other conversations) ==")
    for passed, text in lines:
        print(f"  [{'PASS' if passed else 'FAIL'}] {text}")
    total = _receipt(results)
    print(f"\n{'🟢 ALL CHALLENGES CLOSED' if ok else '🔴 SOME CHALLENGES OPEN — see FAIL rows above'}"
          f"  (real spend ${total:.5f}; full log {out_path})")
    if args.json:
        print("\n" + json.dumps(results, indent=2, default=str))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())

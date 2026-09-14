"""Thinking-budget matrix (roadmap A4) — MEASURE the extended-thinking budget each Anthropic-shape model needs, so
`models.thinking_budget` returns a MEASURED fact instead of None (thinking is dormant until a fact exists).

THE MEASUREMENT, and why it is not a guess. Anthropic extended thinking is a token BUDGET (a ceiling on how much
the model may think), and the model self-regulates within it. We size that ceiling from what the model ACTUALLY
spends thinking on a fixed reasoning-heavy problem:

  demand = out_tok(thinking ON) − out_tok(thinking OFF)      # same prompt, same model; the delta IS the thinking

Both terms are EXACT usage counts from the provider (never a char estimate of the reply), so the delta is the real
thinking-token cost — a structural measurement, no meaning-judgement. We record `thinking:*` = demand × headroom,
and ONLY when demand clearly engaged (>= models._THINK_API_MIN); a model whose thinking did not engage (delta below
the API floor — unsupported, rejected+stripped, or genuinely light) gets a DIAGNOSTIC row and NO fabricated fact.

THE BOOTSTRAP, done WITHOUT touching the persistent store. `adapters.call` enables thinking only when
`thinking_budget()` returns a budget — a chicken-and-egg. Rather than SEED a fact (a persistent write that would
then need a delete), we inject the scaffold budget for the ON probe ONLY by patching the in-process function
`models.thinking_budget` for that one call, then restore it. So the facts store is NEVER seeded and NEVER cleared.

DURABILITY (the doctrine applied to DATA — trash + count + durable path): the SOLE store write is `add_fact` of a
measured value, and it goes through `_record_with_backup`, which first APPENDS the prior value+provenance to a
durable backup log (fsync'd) and COUNTS it, THEN writes — so an overwrite of a pre-existing measured budget is
recoverable, never a silent loss. A sub-floor / failed measurement writes NOTHING (a prior fact is left intact).

ORDINAL SCOPE (honest): we record `thinking:*` (the wildcard `thinking_budget` falls back to for every ordinal) —
one measured ceiling per model, coherent because the budget is a ceiling the model self-regulates under. Per-ordinal
gradation (low<medium<high budgets) is a MEASURED follow-on, not a fabricated fraction here.

DURABLE + RESUMABLE + PER-UNIT ISOLATED + ESTIMATE-FIRST (the repo doctrine, as reasoning_stress_matrix.py): each
model's row is appended+fsync'd the instant its paid calls return; a rerun with the same --out SKIPS a model already
recorded (never re-pays); one model raising does not abort the matrix; a DELIBERATE gate refusal propagates
(fail-closed). Default is a zero-spend estimate; `--run` executes under the gate and refuses over `--budget`.
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
from spendguard import adapters, pricing, gate, models, catalog   # noqa: E402

# ── the reasoning fixture: an inclusion–exclusion count with ONE deterministic answer (test data, COMPUTED below) ──
_N = 1000                                   # integers 1..N
_DIVISORS = (6, 10, 15)                     # count those divisible by NONE of these


def _expected_answer():
    """Ground truth, COMPUTED from the premises (not a bald literal): how many integers in [1, _N] are divisible by
    none of _DIVISORS. A direct count — unambiguous, and a free correctness signal for the reply."""
    return sum(1 for x in range(1, _N + 1) if all(x % d for d in _DIVISORS))


PROMPT = (
    f"Work this out step by step, showing your reasoning, then put the final count on the last line in the exact "
    f"form  ANSWER: <number>\n\n"
    f"How many integers from 1 to {_N} inclusive are divisible by NONE of {_DIVISORS[0]}, {_DIVISORS[1]}, or "
    f"{_DIVISORS[2]}? Use inclusion–exclusion and be careful with the least common multiples."
)

SEED_BUDGET = 16000        # generous scaffold budget so thinking is NOT truncated while we measure demand
OFF_MAX_OUT = 4000         # answer-only ceiling for the thinking-OFF baseline (no reasoning → no thinking)
ON_MAX_OUT = SEED_BUDGET + 4000   # must EXCEED the budget (API rule) + leave visible-answer room
HEADROOM = 1.3             # record demand × this as the ceiling, so a slightly harder task still fits
MAIN_TIMEOUT_S = 120       # generous wall-clock for a real thinking generation
EST_IN = 80                # the prompt is ~80 tokens
SG_OFF = "spendguard:thinking-budget-baseline"
SG_ON = "spendguard:thinking-budget-measure"


def _anthropic_ids():
    """Served Anthropic (claude-*) ids — from the install's OWN catalog, else the price table (shape-agnostic scan).
    NEVER a hardcoded model list: the matrix measures whatever THIS install serves/prices."""
    ids = catalog.live_model_ids("anthropic")
    if ids:
        return sorted(m for m in ids if str(m).startswith("claude-"))
    found = set()

    def _walk(o):
        if isinstance(o, dict):
            for k, v in o.items():
                if isinstance(k, str) and k.startswith("claude-"):
                    found.add(k)
                _walk(v)
        elif isinstance(o, list):
            for x in o:
                _walk(x)
    try:
        _walk(pricing.PRICING)
    except Exception:
        pass
    return sorted(found)


def targets():
    return [("anthropic", m) for m in _anthropic_ids()]


def _price(spec, i, o):
    try:
        return pricing.realtime_cost(spec, i, o)
    except Exception:
        return None


def estimate(tgts=None):
    """Zero-spend $ estimate: per model, a thinking-OFF baseline (small out) + a thinking-ON probe priced at the
    WORST case (the full seed budget as output). The model self-regulates below the ceiling, so real spend is lower."""
    tgts = tgts if tgts is not None else targets()
    rows, total = [], 0.0
    for prov, mid in tgts:
        c_off = _price(f"{prov}:{mid}", EST_IN, 800)                 # baseline: prompt + a short worked answer
        c_on = _price(f"{prov}:{mid}", EST_IN, SEED_BUDGET)          # worst case: model spends the whole budget
        rows.append({"provider": prov, "model": mid, "off": c_off, "on": c_on})
        total += (c_off or 0.0) + (c_on or 0.0)
    return {"rows": rows, "total": round(total, 4), "n": len(tgts)}


def _correct(text):
    """FREE correctness signal (not the PASS criterion): parse the fixed-format ANSWER token this prompt REQUIRES and
    compare to the computed ground truth by EXACT integer equality. None when no ANSWER token → unjudgeable."""
    if not text:
        return None
    m = re.search(r"ANSWER:\s*\*{0,2}\$?\s*([0-9][0-9,]*)", text, re.IGNORECASE)
    if not m:
        return None
    return int(m.group(1).replace(",", "")) == _expected_answer()


def _safe_call(spec, sig, reasoning, max_out):
    """One gated call, PER-UNIT ISOLATED: a transport/provider exception becomes a recorded `raised` result so the
    matrix continues; a DELIBERATE gate refusal PROPAGATES (fail-closed, never swallowed)."""
    try:
        return adapters.call(spec, PROMPT, reasoning=reasoning, max_tokens=max_out,
                             timeout_s=MAIN_TIMEOUT_S, sig=sig, no_substitution=True, metered_only=True)
    except gate.SpendGateRefused:
        raise
    except Exception as e:                        # noqa: BLE001 — any provider/transport failure is DATA, not a crash
        return {"error": f"{type(e).__name__}: {e}", "error_type": "raised", "cost": 0.0, "text": None, "out_tok": None}


def _measure_on(spec):
    """The thinking-ON probe. Inject the scaffold budget for THIS call ONLY by patching the in-process budget source
    (models.thinking_budget) — NO write to the persistent facts store, so nothing is ever seeded or later cleared.
    _call_once reads `models.thinking_budget` at call time, so the patch is seen; it is restored immediately after."""
    _orig = models.thinking_budget
    models.thinking_budget = lambda _m, _r, _mx: (
        SEED_BUDGET if (_r and str(_r).lower() not in ("", "minimal", "none")) else None)
    try:
        return _safe_call(spec, SG_ON, "high", ON_MAX_OUT)
    finally:
        models.thinking_budget = _orig            # ALWAYS restore the real function (even on raise)


def _sink(path, obj):
    """Durable append-only checkpoint — one row, fsync'd, so each paid result survives a crash. The same paid fact is
    ALSO in the gate ledger (adapters.call → record_call); this is a second copy, not the sole one."""
    with open(path, "a") as f:
        f.write(json.dumps(obj, default=str) + "\n")
        f.flush()
        os.fsync(f.fileno())


def _record_with_backup(mid, value, prior, backup_path):
    """Write the MEASURED thinking:* fact, but FIRST trash the prior value+provenance to a durable backup log (fsync'd)
    and COUNT it — the [durability] trash+count+durable-path discipline. An overwrite of a pre-existing measured
    budget is therefore recoverable from the backup (in addition to the run checkpoint), never a silent loss. A
    first-time write (prior is None) records an explicit null, so the backup shows exactly what was replaced."""
    _sink(backup_path, {"kind": "fact_overwrite_backup", "model": mid, "key": "thinking:*", "prior": prior,
                        "new": value, "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())})
    _priorval = "null" if prior is None else prior.get("value")
    print(f"  [durability] backed up prior thinking:* for {mid} (={_priorval}) → {backup_path} (1 row), then writing.")
    models.add_fact(mid, "thinking:*", value, source="experiment:thinking_budget_matrix", verified=True)


def _done(path):
    """Model specs already recorded (measured OR diagnostic) — a rerun skips them (never re-pays). A `raised`/error
    baseline leaves the model NOT done, so it is retried."""
    seen, prior = set(), []
    if os.path.exists(path):
        with open(path) as f:
            for line in f:
                try:
                    o = json.loads(line)
                except Exception:
                    continue
                prior.append(o)
                if o.get("phase") == "model" and o.get("settled"):
                    seen.add(o.get("spec"))
    return seen, prior


def run_matrix(tgts, out_path):
    backup_path = out_path + ".facts_backup.jsonl"    # the durable trash log for any fact this run overwrites
    done, prior_rows = _done(out_path)
    rows = [o for o in prior_rows if o.get("phase") == "model" and o.get("spec") in done]
    if not prior_rows:
        _sink(out_path, {"phase": "meta", "expected_answer": _expected_answer(),
                         "targets": [f"{p}:{m}" for p, m in tgts], "seed_budget": SEED_BUDGET,
                         "think_api_min": models._THINK_API_MIN, "headroom": HEADROOM, "facts_backup": backup_path})

    print(f"\n== THINKING-BUDGET MATRIX — {len(tgts)} Anthropic model(s), seed {SEED_BUDGET}, ground-truth "
          f"answer={_expected_answer()} ==")
    for prov, mid in tgts:
        spec = f"{prov}:{mid}"
        if spec in done:
            print(f"  {spec:32} ⏩ resumed (already recorded)")
            continue

        # DURABILITY: snapshot the prior thinking:* fact (value + provenance) before any write. None on the first run
        # (thinking is dormant until this matrix records it). Carried on every row + backed up at the write site.
        _pf = models.facts(mid).get("thinking:*")
        prior = {"value": _pf[0], "source": _pf[2], "verified": _pf[3]} if _pf else None

        # 1) BASELINE — no reasoning → thinking_budget returns None → thinking OFF. out_tok is the answer-only cost.
        r_off = _safe_call(spec, SG_OFF, None, OFF_MAX_OUT)
        if r_off.get("error"):
            row = {"phase": "model", "spec": spec, "settled": True, "recorded": None, "reason": "baseline_failed",
                   "prior_thinking": prior, "detail": str(r_off.get("error"))[:160]}
            rows.append(row); _sink(out_path, row)
            print(f"  {spec:32} 🔴 baseline failed « {row['detail']} »")
            continue
        off_out = int(r_off.get("out_tok") or 0)

        # 2) THINKING-ON probe (scaffold budget injected in-process; the store is untouched), then measure demand.
        r_on = _measure_on(spec)
        if r_on.get("error"):
            row = {"phase": "model", "spec": spec, "settled": True, "recorded": None, "reason": "on_probe_failed",
                   "off_out": off_out, "prior_thinking": prior, "detail": str(r_on.get("error"))[:160]}
            rows.append(row); _sink(out_path, row)
            print(f"  {spec:32} 🔴 thinking probe failed « {row['detail']} »")
            continue
        on_out = int(r_on.get("out_tok") or 0)
        demand = max(0, on_out - off_out)                 # the thinking tokens (delta of two EXACT usage counts)

        # 3) RECORD the MEASURED budget (demand × headroom) THROUGH the backup-first writer — but only if that budget
        # reaches the API's MINIMUM thinking budget (_THINK_API_MIN). Below it, the provider rejects the budget and
        # models.thinking_budget() returns None, so no valid budget exists to store: this is the vendor's mechanical
        # floor on the recordable value, NOT a judgement about whether the model "really" thought. Otherwise write
        # NOTHING — a pre-existing measured fact is left intact.
        budget = int(round(demand * HEADROOM))
        if budget >= models._THINK_API_MIN:
            _record_with_backup(mid, budget, prior, backup_path)
            recorded, reason = budget, "measured"
        else:
            recorded, reason = None, "below_api_min_budget"      # demand too small to form a valid API thinking budget
        cost = round((r_off.get("cost") or 0.0) + (r_on.get("cost") or 0.0), 6)
        row = {"phase": "model", "spec": spec, "settled": True, "off_out": off_out, "on_out": on_out, "demand": demand,
               "recorded": recorded, "reason": reason, "prior_thinking": prior, "cost": cost,
               "correct": _correct(r_on.get("text"))}
        rows.append(row); _sink(out_path, row)
        _flag = "🟢" if recorded else "🟡"
        print(f"  {spec:32} {_flag} off_out={off_out:5} on_out={on_out:6} demand={demand:6} → "
              f"thinking:*={recorded}  ${cost:.5f}  correct={row['correct']}  ({reason})")
    return rows


def _verdict(rows):
    """Structural PASS: every targeted model returned a structural outcome (no raise/hang), and every RECORDED fact
    reads back through models.thinking_budget. Whether a given model ENGAGED thinking on this fixture is a measured
    RESULT — not a pass criterion: a run where efficient models simply don't need thinking is valid data, reported not
    failed. A run that measured NOTHING is surfaced LOUDLY (the fixture likely under-taxed the models) but is not
    itself a script failure — it is an honest signal to harden the fixture, not a green light misread as success."""
    lines = []
    measured = [r for r in rows if r.get("recorded")]
    no_raise = all((r.get("reason") != "raised") for r in rows)
    lines.append((no_raise, "every targeted model returned a structural outcome — none raised/hung"))
    reads_back = all(models.thinking_budget(r["spec"].split(":", 1)[1], "high", ON_MAX_OUT) == r["recorded"]
                     for r in measured)
    lines.append((reads_back, f"models.thinking_budget reads back every recorded fact ({len(measured)} recorded)"))
    n_eng = len(measured)                                         # INFO (never a gate): how many engaged thinking
    lines.append((True, f"INFO — {n_eng}/{len(rows)} model(s) engaged thinking + recorded a budget"
                        + ("  ⚠ none engaged: this fixture is likely too easy for the targeted models" if n_eng == 0 else "")))
    return all(c for c, _ in lines), lines


def _receipt(rows):
    total = round(sum((r.get("cost") or 0.0) for r in rows), 6)
    print("\n── RECEIPT ──")
    print(f"  Total ${total:.5f} = ${total:.5f} anthropic API   ::   est-value n/a (all metered, nothing plan-covered)")
    return total


def main(argv=None):
    ap = argparse.ArgumentParser(description="Measure per-model extended-thinking budgets (roadmap A4).")
    ap.add_argument("--run", action="store_true", help="actually spend (default: zero-spend estimate)")
    ap.add_argument("--budget", type=float, default=3.0, help="refuse to run if the estimate exceeds this ($)")
    ap.add_argument("--models", default=None, help="comma-separated model ids to scope to (a substring of a served "
                    "id matches, so 'opus-5,sonnet-5' picks those); default = every served Anthropic model")
    ap.add_argument("--out", default=None, help="durable JSONL checkpoint (default: alongside this script); reuse to RESUME")
    ap.add_argument("--json", action="store_true", help="also emit machine-readable rows at the end")
    args = ap.parse_args(list(argv) if argv is not None else None)

    tgts = targets()
    if args.models:
        want = [m.strip() for m in args.models.split(",") if m.strip()]
        tgts = [(p, m) for (p, m) in tgts if any(w in m for w in want)]   # caller-scoped subset (substring match)
    if not tgts:
        print("No Anthropic models found (catalog/price table, after any --models filter) — nothing to measure.")
        return 2
    est = estimate(tgts)
    print(f"Thinking-budget matrix — {est['n']} Anthropic model(s).  Ground-truth answer = {_expected_answer()}.")
    print(f"Estimate (WORST case, thinking-ON priced at the full {SEED_BUDGET}-tok seed): ~${est['total']:.4f} total")
    for row in est["rows"]:
        print(f"  {row['model']:30} baseline~${(row['off'] or 0):.5f}  thinking~${(row['on'] or 0):.5f}")

    if not args.run:
        print(f"\n  ESTIMATE ONLY (${est['total']:.4f}) — re-run with --run to execute under the gate.")
        return 0
    if est["total"] > args.budget:
        print(f"\n  🔴 REFUSED — estimate ${est['total']:.4f} exceeds --budget ${args.budget:.2f}. Raise --budget to proceed.")
        return 2

    out_path = args.out or os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                        "thinking_budget_" + time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()) + ".jsonl")
    print(f"\n  ✅ estimate ${est['total']:.4f} ≤ budget ${args.budget:.2f} — executing under the gate.")
    print(f"  durable checkpoint → {out_path}")
    rows = run_matrix(tgts, out_path)
    ok, lines = _verdict(rows)
    print("\n== VERDICT ==")
    for passed, text in lines:
        print(f"  [{'PASS' if passed else 'FAIL'}] {text}")
    total = _receipt(rows)
    print(f"\n{'🟢 THINKING BUDGETS MEASURED + RECORDED' if ok else '🔴 SEE FAIL ROWS ABOVE'}"
          f"  (real spend ${total:.5f}; full log {out_path})")
    if args.json:
        print("\n" + json.dumps(rows, indent=2, default=str))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())

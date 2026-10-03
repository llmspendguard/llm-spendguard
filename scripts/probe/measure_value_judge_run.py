#!/usr/bin/env python
"""A2.1 value-judge BEFORE/AFTER measurement — the RUN harness (companion to the $0 measure_value_judge_estimate.py).

QUESTION it answers: does the A2.1 VALUE judge (quality weighed ~70% against realized cost ~30%) pick the LEAN arm
more than a pure-QUALITY judge would, WITHOUT sacrificing quality? For M real tasks of a target intent:
  • generate a LEAN-arm and a HEAVY-arm output (the two arms DERIVED from the intent's recorded output distribution —
    the lowest- and highest-mean-output measured arms, e.g. codex:gpt-5.6-sol ~13 tok vs zai-coding:glm-5.3 ~510 tok);
  • BEFORE pick = the judge on the PURE-QUALITY prompt (no costs); AFTER pick = the SAME judge on the VALUE prompt
    (the arms' realized $/call) — the before/after contrast is exactly A2.1's effect;
  • an INDEPENDENT quality GRADE of each output, so a shift to the lean arm is only a win if quality held.

WHY THIS HARNESS EXISTS (the malfunction it fixes). A pilot FANNED OUT to ~1100 calls because the judge went through
lane_bandit.bakeoff_judge, whose `adapters.call` is UN-caged — under advisor.default_reasoning=best-value a cold
measure-intent call triggers the bandit's own bake-off fan-out. So here EVERY llm call is HARD-CAGED: the judge/grade
run on the metered judge model with metered_only=True + reasoning pinned + no_substitution=True — exactly one metered,
billable, un-routable call each (we reuse lane_bandit._value_judge_prompt to BUILD the prompt, but make the CALL
ourselves, never bakeoff_judge). The arm generation is a DELIBERATE lane_bandit._run_arm on the exact arm (no routing,
no fallback — $0, plan-served). And the spend cap reads the REAL LEDGER DELTA (budget.spent_by_job on this run's chain
tag — the measurement's own recorded billed $), NOT estimated-unit × count, and ABORTS the moment it is crossed.

ESTIMATE-FIRST + FAIL-CLOSED (the API-spend protocol): the default is a $0 PLAN + ESTIMATE; `--run` REQUIRES an
explicit `--budget` (the ledger-delta cap). Run UNDER the gate.
"""
import argparse
import datetime

import spendguard
spendguard.require()                                   # fail closed — refuse if the gate is not enforcing here

from spendguard import calls, lane_bandit, budget

MEASURE_INTENT = "measure:value-judge"                 # all caged calls are tagged with THIS (attribution + the cap read)


def _is_lane_arm(executor):
    """True iff `executor` is a subscription LANE (lane_bandit._run_arm can regenerate on it), NOT a metered provider
    ('api' / 'openai' / 'anthropic'). The measurement REGENERATES both arms via _run_arm, so BOTH must be lanes —
    a metered arm would come back blank and silently skip every pair. $0; False on any error."""
    try:
        from spendguard import lane_catalog, adapters
        prov = lane_catalog.lane_provider(executor)
        return bool(prov and adapters._LANES.get(prov))
    except Exception:
        return False


def _derive_arms(target, min_arm_n=2):
    """(lean_arm, heavy_arm) for `target`, DERIVED from its recorded output distribution — the lowest- and highest-
    mean-output measured LANE arms (exec is a subscription lane, NOT a metered provider) with ≥ min_arm_n calls. Each
    arm is (lane, use_name) as lane_bandit keys it; restricting to lanes is REQUIRED because _run_arm regenerates on a
    lane. None if the intent lacks two comparable LANE arms (the measurement needs a lean-vs-heavy contrast both arms
    can produce). Reads recorded data only ($0); never invents an arm."""
    norms = calls.mean_out_by_executor_model(target) or {}
    arms = [(k, v) for k, v in norms.items()
            if (v.get("mean_out") or 0) > 0 and (v.get("n") or 0) >= min_arm_n and _is_lane_arm(k[0])]
    if len(arms) < 2:
        return None
    arms.sort(key=lambda kv: kv[1]["mean_out"])
    (lean_k, lean), (heavy_k, heavy) = arms[0], arms[-1]
    return {"lean": {"arm": lean_k, "mean_out": round(lean["mean_out"]), "n": lean["n"]},
            "heavy": {"arm": heavy_k, "mean_out": round(heavy["mean_out"]), "n": heavy["n"]}}


def _grade_prompt(task, answer):
    """The INDEPENDENT quality grade prompt — a tiny fixed-shape reply so the grade call is as cheap + bounded as the
    judge. Not a bakeoff: it scores ONE answer's quality for the task on a 0-10 scale, so a value-pick's quality can be
    checked separately from which arm the judge preferred."""
    return ("Score how well this answer accomplishes the task, 0-10 (10 = fully correct, complete, on-format). Reply "
            "with ONLY the integer.\n\nTASK:\n%s\n\n=== ANSWER ===\n%s\n" % (task, answer))


def _caged_call(prompt, run_chain):
    """ONE hard-caged metered call for a judge/grade — metered_only + reasoning pinned + no_substitution, tagged with
    the measurement intent AND this run's chain so budget.spent_by_job(run_chain) reads its exact billed $. This is the
    whole anti-fan-out fix: predictable (one call), billable (metered), un-routable (no bandit/best-value swap)."""
    from spendguard import adapters
    judge = lane_bandit._bandit_judge_model()          # the cheap judge model (haiku) — honors 'minimal'
    with calls.context(intent=MEASURE_INTENT, chain=run_chain):
        r = adapters.call(judge, prompt, metered_only=True, reasoning="minimal", no_substitution=True)
    return (r.get("text") or "").strip()


def _pick(txt):
    """Map the judge's reply to the picked arm by EXACT match on the ONE WORD it was instructed to give — 'A' = lean,
    'B' = heavy, 'TIE' = tie — a validated fixed enum, never a prefix guess (which could read 'Actually, B' as A).
    Trailing punctuation is stripped (the format token, not meaning). ANY other reply means the judge went off-format;
    it is 'unparseable' — its own recorded bucket, never silently counted as a pick."""
    tok = (txt or "").strip().upper().strip(".:,)")
    if tok == "A":
        return "lean"
    if tok == "B":
        return "heavy"
    if tok == "TIE":
        return "tie"
    return "unparseable"


def plan(target, Ms=(10, 15, 20)):
    """$0 PLAN + ESTIMATE for `target`: the derived arms, the exact caged call plan, and the billed-$ estimate at a few
    staged M (never a large batch). Reads recorded data + pricing only — NO llm call, nothing spent."""
    print("== A2.1 value-judge BEFORE/AFTER measurement — PLAN + ESTIMATE (zero spend) ==\n")
    print("target intent: %r" % target)
    arms = _derive_arms(target)
    if not arms:
        print("  NOT MEASURABLE: %r has < 2 comparable recorded arms. Pick an intent with a lean-vs-heavy pair "
              "(see measure_value_judge_estimate.py's output-spread evidence)." % target)
        return 1
    print("  arms (derived from recorded output):  LEAN %s ~%d tok (n%d)  vs  HEAVY %s ~%d tok (n%d)" % (
        arms["lean"]["arm"], arms["lean"]["mean_out"], arms["lean"]["n"],
        arms["heavy"]["arm"], arms["heavy"]["mean_out"], arms["heavy"]["n"]))
    spread = (arms["heavy"]["mean_out"] / arms["lean"]["mean_out"]) if arms["lean"]["mean_out"] else 0.0
    print("  output spread: %.0fx (the cost signal A2.1's value judge weighs; whether it is WASTE is what we measure)" % spread)

    judge = lane_bandit._bandit_judge_model()
    jc = lane_bandit.estimate_judge_cost()
    per = jc.get("per_bakeoff_usd")
    # a grade reply is one integer — its prompt (task + one answer) is smaller than the judge's (task + TWO answers);
    # bound it at the judge's per-call cost (conservative UPPER bound), so the estimate never under-counts.
    print("\n  HARD-CAGE: judge + grade run on %r with metered_only=True + reasoning='minimal' + no_substitution=True —"
          " one metered, billable, UN-ROUTABLE call each (no bandit/best-value fan-out). Arm generation is a deliberate"
          "\n  lane_bandit._run_arm on the exact arm ($0, plan-served). CAP: budget.spent_by_job(<run chain>) — the REAL"
          " ledger delta of this run's own calls — aborts the instant it is crossed." % judge)
    print("\n  per caged call ≈ $%s (in≈%s tok bound, out cap %s)" % (
        ("%.5f" % per) if per is not None else "—", jc.get("in_tok_bound"), jc.get("out_tok_cap")))
    print("\n  ESTIMATE — per pair: BEFORE judge + AFTER judge + LEAN grade + HEAVY grade = 4 billed caged calls (both"
          " arms graded so 'quality held' is comparable); 2 lane arm-gen calls ($0 billed, plan-served). Staged M:")
    print("   M pairs | billed caged calls | est $ (billed, UPPER bound) | lane arm-gen ($0 billed / plan usage)")
    for M in Ms:
        n = 4 * M
        tot = (per * n) if per is not None else None
        print("   %7d | %18d | %26s | %d calls" % (M, n, (("$%.4f" % tot) if tot is not None else "—"), 2 * M))
    print("\n  To RUN (spends): approve a concrete M, then:\n"
          "    scripts/probe/measure_value_judge_run.py --target %r --run --m <M> --budget <$>\n"
          "  --budget is the hard ledger-delta cap; the run ABORTS (records partial) the moment spent_by_job exceeds it." % target)
    print("\n  Nothing was spent. This is the estimate for your approval (API-spend protocol: test → estimate → APPROVE → run).")
    return 0


def _parse_arm(spec):
    """A caller-supplied arm 'lane:use_name' → (lane, use_name). Lets the caller pin a KNOWN-GENERATABLE pair when the
    auto-derived one includes an arm the lane can't reproduce (e.g. a bare gemini model id the lane rejects without an
    effort suffix). PARSING a fixed 'lane:use_name' shape, not a decision."""
    lane, _sep, use_name = str(spec).partition(":")
    if not lane or not use_name:
        raise SystemExit("--lean/--heavy must be 'lane:use_name' (e.g. codex:gpt-5.6-sol) — got %r" % spec)
    return (lane, use_name)


def run(target, M, budget_usd, src_files=None, lean=None, heavy=None):
    """EXECUTE the measurement for `target` at M pairs under a hard ledger-delta cap `budget_usd`. HARD-CAGED (judge +
    grade metered+pinned+no_substitution; arm-gen deliberate _run_arm) and FAIL-CLOSED: a pair starts only if the REAL
    billed delta budget.spent_by_job(run_chain) + the pair's worst case still fits the cap. `lean`/`heavy` (each
    'lane:use_name') pin the arms when the auto-derived pair isn't generatable; default = _derive_arms. Returns a
    summary {before, after, grades, pairs_done, billed_usd, aborted}."""
    if not budget_usd or budget_usd <= 0:
        raise SystemExit("run REQUIRES a positive --budget (the ledger-delta cap) — the estimate-first API-spend protocol")
    if lean and heavy:
        lean_arm, heavy_arm = _parse_arm(lean), _parse_arm(heavy)
    elif lean or heavy:
        raise SystemExit("pass BOTH --lean and --heavy, or neither (then the arms are derived)")
    else:
        arms = _derive_arms(target)
        if not arms:
            raise SystemExit("%r has < 2 comparable recorded LANE arms — not measurable; pin --lean/--heavy explicitly" % target)
        lean_arm, heavy_arm = arms["lean"]["arm"], arms["heavy"]["arm"]
    tasks = _review_tasks(M, src_files)                # M real .py review prompts (honestreview coding_router), WHOLE
    if len(tasks) < M:
        print("  only %d review tasks available (< M=%d) — measuring the %d available (no padding)." % (len(tasks), M, len(tasks)))
    run_chain = "measure-value-judge-%s" % datetime.datetime.now().strftime("%Y%m%dT%H%M%S")
    print("== A2.1 value-judge measurement RUN — target %r, %d pair(s), cap $%.4f, chain %s ==" % (
        target, len(tasks), budget_usd, run_chain))
    # The pair is ATOMIC w.r.t. the cap: start one ONLY if the REAL ledger delta (spent_by_job) PLUS this pair's
    # worst-case billed cost still fits the budget. So a started pair always COMPLETES within budget — no overshoot
    # (it was pre-cleared) AND no task half-spent-then-dropped (its four metered calls run together or not at all).
    # Worst-case pair cost = 4 caged calls at the judge's priced UPPER bound; refuse if that is unpriceable (no
    # enforceable cap without a price — fail-closed).
    per = lane_bandit.estimate_judge_cost().get("per_bakeoff_usd")
    if per is None:
        raise SystemExit("cannot price the judge model %r → cannot bound a pair's cost → refusing to run without an "
                         "enforceable cap (price it first: `spendguard sync-prices`)" % lane_bandit._bandit_judge_model())
    pair_cost = 4.0 * float(per)
    before = {"lean": 0, "heavy": 0, "tie": 0, "unparseable": 0}
    after = {"lean": 0, "heavy": 0, "tie": 0, "unparseable": 0}
    grades, done, aborted = [], 0, False
    for i, task in enumerate(tasks):
        spent = float(budget.spent_by_job(run_chain))        # REAL ledger delta of THIS run so far
        if spent + pair_cost > budget_usd:
            aborted = True
            print("  CAP: the next pair's worst case (~$%.4f) would push spent $%.4f over budget $%.4f — stopping at %d "
                  "completed pair(s) (none dropped mid-pair)." % (pair_cost, spent, budget_usd, done))
            break
        lean_out, _lt = lane_bandit._run_arm(lean_arm, task, None, None, None)   # deliberate arm-gen, $0 lane, no routing
        heavy_out, _ht = lane_bandit._run_arm(heavy_arm, task, None, None, None)
        if not lean_out or not heavy_out:
            print("  pair %d: an arm returned blank (lane down?) — skipped, not counted." % i)
            continue
        costs = lane_bandit._intent_realized_costs(target, [lean_arm, heavy_arm])
        # the pair's four caged calls run ATOMICALLY — pre-cleared to fit the budget above, so no mid-pair abort.
        p_before = _pick(_caged_call(lane_bandit._value_judge_prompt(task, lean_out, heavy_out), run_chain))
        p_after = _pick(_caged_call(lane_bandit._value_judge_prompt(task, lean_out, heavy_out, costs.get(lean_arm), costs.get(heavy_arm)), run_chain))
        g_lean = _caged_call(_grade_prompt(task, lean_out), run_chain)
        g_heavy = _caged_call(_grade_prompt(task, heavy_out), run_chain)
        before[p_before] += 1
        after[p_after] += 1
        grades.append({"lean": g_lean, "heavy": g_heavy, "before": p_before, "after": p_after})
        done += 1
    billed = float(budget.spent_by_job(run_chain))
    print("\n== RESULT ==")
    print("  pairs measured: %d%s" % (done, " (ABORTED at the cap)" if aborted else ""))
    print("  BEFORE (pure-quality judge) picks:  lean %d · heavy %d · tie %d · unparseable %d"
          % (before["lean"], before["heavy"], before["tie"], before["unparseable"]))
    print("  AFTER  (value judge, cost-aware):   lean %d · heavy %d · tie %d · unparseable %d"
          % (after["lean"], after["heavy"], after["tie"], after["unparseable"]))
    print("  → A2.1 effect: the value judge shifted %+d pick(s) toward the LEAN arm vs pure-quality." % (after["lean"] - before["lean"]))
    print("  independent grades (lean/heavy, 0-10) per pair:")
    for g in grades:
        print("     before=%-5s after=%-5s  lean=%s heavy=%s" % (g["before"], g["after"], g["lean"], g["heavy"]))
    print("\n  BILLED (real ledger, this run's chain): $%.4f  (cap $%.4f)" % (billed, budget_usd))
    print("  Interpret: a shift to LEAN is a WIN only if the lean grades held up to the heavy ones (read the grades above).")
    return {"before": before, "after": after, "grades": grades, "pairs_done": done, "billed_usd": billed, "aborted": aborted}


def _review_tasks(M, src_files=None):
    """M real code-review tasks — honestreview.coding_router's EXACT review prompt over spendguard's own .py (the WHOLE
    file each, never a slice; the review is the realistic intent with a measured lean-vs-heavy output spread). Falls
    back to a clear message if honestreview is not importable (the arm-gen needs its prompt + rules)."""
    import os
    try:
        from honestreview import coding_router
    except Exception as e:
        raise SystemExit("the run needs honestreview.coding_router for the review prompt (%s) — install it, or pass "
                         "--src to point at .py files and adapt _review_tasks" % type(e).__name__)
    rules = coding_router.family_rules("python") or ""
    root = src_files or os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "src", "spendguard")
    files = []
    for dp, _dn, fns in os.walk(root):
        for fn in sorted(fns):
            if fn.endswith(".py"):
                files.append(os.path.join(dp, fn))
    files = sorted(files)[:M]
    tasks = []
    for f in files:
        with open(f) as fh:
            content = fh.read()                        # WHOLE file — the review evidence is sent whole
        tasks.append("%s\n\nHere is proposed SOURCE being written. Review it against the rules.\n\n```\n%s\n```" % (rules, content))
    return tasks


def main(argv=None):
    ap = argparse.ArgumentParser(prog="measure_value_judge_run",
                                 description="A2.1 value-judge before/after measurement — $0 plan/estimate by default; --run spends (needs --budget).")
    ap.add_argument("--target", default="honestreview:coding_router", help="the intent to measure (default: honestreview:coding_router)")
    ap.add_argument("--run", action="store_true", help="EXECUTE (default: $0 plan + estimate only)")
    ap.add_argument("--m", type=int, default=None, help="pairs to measure (REQUIRED with --run)")
    ap.add_argument("--budget", type=float, default=None, help="hard ledger-delta cap in $ (REQUIRED with --run)")
    ap.add_argument("--src", default=None, help="dir of .py review tasks (default: spendguard's own src)")
    ap.add_argument("--lean", default=None, help="pin the LEAN arm 'lane:use_name' (default: auto-derived lowest-output lane arm)")
    ap.add_argument("--heavy", default=None, help="pin the HEAVY arm 'lane:use_name' (default: auto-derived highest-output lane arm)")
    a = ap.parse_args(argv)
    if not a.run:
        return plan(a.target)
    if a.m is None or a.budget is None:
        ap.error("--run REQUIRES --m <pairs> and --budget <$> (the estimate-first ledger-delta cap)")
    run(a.target, a.m, a.budget, src_files=a.src, lean=a.lean, heavy=a.heavy)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

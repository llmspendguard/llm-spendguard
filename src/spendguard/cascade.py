"""Cost-aware cascade routing (FrugalGPT / AutoMix idea, re-implemented) — try the CHEAP model first,
verify, and escalate to a stronger model only when the cheap answer fails verification. Most queries get
served cheap; the expensive model is reserved for the hard ones.

    res = cascade.cascade(prompt, ["gpt-5-nano", "gpt-5-mini", "gpt-5.5"], verify=my_check, intent="...")
    # res: {model, output, cost, escalations, n_tried} — model that ultimately served + total spend

The VERIFIER is the quality gate: a weak verifier accepts wrong cheap output (the whole risk), so for
quality-sensitive intents pass a real one (JSON-schema check, a confidence rating, a cheap LLM judge).
Default = non-empty AND (if the prompt looks like it wants JSON) it parses. Skips models already known
ineffective for the intent (the denylist). WORKLOAD spend (real intent → your caps). Per-model params
(reasoning etc.) auto-applied via models.apply_call_params inside the caller.
"""
import re
from . import equivalence, models as M


def _wants_json(prompt):
    return bool(re.search(r"\bJSON\b|\{|\[", prompt or "")) and "json" in (prompt or "").lower()


def default_verify(prompt, output):
    """Free, conservative: non-empty, and if the task asked for JSON it must parse. Replace for quality
    work — this only catches empty/broken output, not subtly-wrong cheap answers."""
    if not output or not output.strip():
        return False
    if _wants_json(prompt):
        return equivalence._norm_json(output) is not None
    return True


def _default_caller(model, prompt):
    from . import experiment
    cost, _it, _ot, text = experiment._call(model, prompt, max_out=1500)
    return cost, text


def cascade(prompt, ladder, verify=None, intent=None, _caller=None):
    """Route prompt down the ladder (cheapest→strongest). Returns dict with the serving model + costs."""
    verify = verify or default_verify
    caller = _caller or _default_caller
    usable = [m for m in ladder if not (intent and M.ineffective(m, intent))]
    skipped = [m for m in ladder if m not in usable]
    escalations, total, served, out = [], 0.0, None, ""
    for i, m in enumerate(usable):
        cost, out = caller(m, prompt)
        total += cost
        served = m
        if verify(prompt, out) or i == len(usable) - 1:   # accept on pass, or the last rung as fallback
            break
        escalations.append(m)                              # this rung failed verification → escalate
    # what the strongest rung alone would have cost (rough: the last successful call's cost as proxy)
    return dict(model=served, output=out, cost=round(total, 6), escalations=escalations,
                skipped_ineffective=skipped, n_tried=len(escalations) + 1)


def cmd(argv=None):
    import argparse, sys
    ap = argparse.ArgumentParser(prog="spendguard cascade")
    ap.add_argument("--ladder", required=True, help="cheap→strong, comma-separated (e.g. gpt-5-nano,gpt-5-mini,gpt-5.5)")
    ap.add_argument("--prompt", help="prompt to route")
    ap.add_argument("--prompt-file")
    ap.add_argument("--intent", help="skip models known-ineffective for this intent")
    ap.add_argument("--run", action="store_true", help="actually call (WORKLOAD spend). Else just shows the ladder.")
    a = ap.parse_args(list(sys.argv[2:] if argv is None else argv))
    ladder = [m.strip() for m in a.ladder.split(",") if m.strip()]
    usable = [m for m in ladder if not (a.intent and M.ineffective(m, a.intent))]
    print(f"cascade — ladder {ladder}" + (f"  (intent '{a.intent}')" if a.intent else ""))
    print("  ⚠ default verifier only catches EMPTY/broken output — it does NOT detect a subtly-wrong cheap "
          "answer. For quality-sensitive work pass a real verify() (schema / confidence / cheap judge) in code.")
    for m in ladder:
        bad = M.ineffective(m, a.intent) if a.intent else None
        print(f"  {'⊘' if bad else '·'} {m}" + (f"  (skipped — ineffective: {bad[0]})" if bad else ""))
    if not a.run:
        print("  estimate/preview only — pass --run with --prompt/--prompt-file to route a real call (WORKLOAD spend).")
        return 0
    prompt = a.prompt or (open(a.prompt_file).read() if a.prompt_file else None)
    if not prompt:
        print("  need --prompt or --prompt-file with --run."); return 1
    from . import calls
    with calls.context(intent=a.intent):              # WORKLOAD (real intent → your caps)
        r = cascade(prompt, usable, intent=a.intent)
    print(f"\n  served by: {r['model']}  ·  tried {r['n_tried']} rung(s)  ·  escalated through {r['escalations']}"
          f"  ·  cost ${r['cost']:.5f}")
    print(f"  output: {(r['output'] or '')[:200]}")
    return 0

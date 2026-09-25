"""Multi-provider PANEL REVIEW of the conformance matrix — "are these the right tests?" reviewed by a DIVERSE cross-
vendor panel, not one reviewer sharing one set of blind spots. It dogfoods spendguard's own capability: the panel runs
via bulk_delegate(metered_only=True) so each vendor answers as ITSELF (no lane-collapse) — the exact B10/B14 behaviour.

Estimate-first, like everything: `--estimate` prices the panel (0 spend) so the $ is approved BEFORE any call. The
review EVIDENCE is the REAL source of truth fed WHOLE — the spec (CONFORMANCE.md), the manifest (behaviours.py) and the
directive (GUARDRAILS_reasoning_overspend.md) — never a hand-written summary that could drift from what actually runs,
and never truncated (the evidence-truncation rule: a reviewer must see the whole thing or its verdict is uninformed).

Each reviewer answers a fixed RUBRIC and the critiques are written durably; the synthesis is a human read, then the
matrix is finalised. No verdict here is auto-applied — the panel INFORMS the matrix, a human decides."""
import json
import os
import sys
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO = os.path.abspath(os.path.join(_HERE, "..", "..", ".."))

# The review EVIDENCE — the real artefacts, fed whole. Paths are repo-relative so this is not machine-specific.
EVIDENCE_FILES = [
    "docs/CONFORMANCE.md",                                 # the suite spec (why it exists, principles, budget model)
    "scripts/integration/conformance/behaviours.py",       # the actual manifest the run reads (id/tier/incident/assertion/why)
    "docs/GUARDRAILS_reasoning_overspend.md",              # the A–E directive (the intent the matrix must cover)
]

RUBRIC = """You are one reviewer on a diverse panel auditing a CONFORMANCE TEST SUITE for `llm-spendguard`, a tool whose
job is to GOVERN LLM spend: estimate-first + approval-gated, admit/pace so callers never see a 429, lanes with
lane→metered→base failover, pin the model for cross-vendor panels (metered_only), reasoning economics (reasoning tokens
bill as OUTPUT; don't cut a reasoning call mid-thought; detect the cut when it happens), a REAL running budget cap, a
per-call runaway breaker, honor-or-refuse an explicit effort pin, and NEVER fail open on a spend refusal.

Below is the SUITE, WHOLE: its spec, its behaviour manifest (each behaviour has an id, a tier = hard-stop run order, the
incident it guards, how it's induced, the surface `assertion` a pass reads, and a `why`), and the guardrails directive
it must cover. Review it HARD and answer ONLY this rubric, as JSON:

{
  "missing": [ {"intent": "<a spendguard behaviour/intent NOT covered by any B#>", "why_it_matters": "...", "suggested_assertion": "..."} ],
  "weak_assertions": [ {"id": "B#", "problem": "why the stated assertion does NOT actually prove the intent (vacuous, passes trivially, or measures the wrong surface)", "stronger_assertion": "..."} ],
  "redundant_or_misscoped": [ {"id": "B#", "issue": "..."} ],
  "priority_problems": [ {"id": "B#", "issue": "wrong tier for a hard-stop run — a critical proof that would be sacrificed by a budget cut, or a cheap one starved late", "fix": "..."} ],
  "could_slip_through": [ "a concrete real-world overspend / crash / silent-wrong-answer this suite would still MISS" ],
  "verdict": "<one paragraph: does this suite prove spendguard lives up to its intent ACROSS THE BOARD? what is the single most important gap?>"
}

Be specific and cite B# ids. Prefer finding a real gap over reassurance — an empty list where a gap exists is the failure mode."""


def build_payload():
    """The full review prompt: the rubric + every evidence file, WHOLE, each fenced and labelled. Never truncated."""
    parts = [RUBRIC, "", "=" * 80, "THE SUITE UNDER REVIEW (complete, unabridged):"]
    for rel in EVIDENCE_FILES:
        p = os.path.join(_REPO, rel)
        try:
            with open(p, "r", errors="ignore") as f:
                body = f.read()
        except OSError as e:
            body = f"<<could not read {rel}: {e}>>"
        parts += ["", "=" * 80, f"### FILE: {rel}", "", body]
    return "\n".join(parts)


def panel_models():
    """The cross-vendor panel — a TUNABLE default (never a hardcode in logic). Reuses spendguard's own configured
    cross-LLM default panel (ask.default_vendors) when set, else a documented diverse fallback; env overrides."""
    env = os.getenv("SPENDGUARD_CONFORMANCE_PANEL")
    if env:
        return [m.strip() for m in env.split(",") if m.strip()]
    try:
        from spendguard import config
        cfg = config._cfg_get("ask", "default_vendors", None)
        if cfg:
            return [m.strip() for m in cfg.split(",") if m.strip()]
    except Exception:
        pass
    return ["anthropic:claude-opus-4-8", "openai:gpt-5.5", "moonshot:kimi-k3", "google:gemini-3-flash"]


def _rough_tokens(text):
    return max(1, len(text) // 4)                          # ~4 chars/token — a conservative count for an estimate only


_PANEL_DEADLINE_DEFAULT_S = 600.0   # fallback ONLY when a model has too few measurements (< 5 obs) for the latency
#                                     advisor. Named, not a literal: a thorough reasoning review of the WHOLE suite is a
#                                     long call, so the unmeasured default is generous — then clamped by
#                                     adapters.deadline_for to [30s, 1800s].


def resolve_panel_deadline(models, payload, explicit=None):
    """The batch wall-clock deadline (seconds), SIZED FROM MEASURED per-model latency — never a hardcode. bulk_delegate
    bounds the WHOLE batch with a single deadline, so it must fit the SLOWEST reviewer: a reasoning model on the full
    payload lands in the slow tail (measured kimi-k3 p95 ≈ 265s vs the old hardcoded 180s), and too tight a deadline
    tears it down MID-THOUGHT — which still BILLS (reasoning tokens, no output) while the local ledger reads $0. Sizing
    lives in exactly ONE place, adapters.deadline_for; here we only take the MAX across the panel. Precedence: explicit
    arg → $SPENDGUARD_CONFORMANCE_DEADLINE_S → measured advisor → the named default. Returns (seconds, basis)."""
    if explicit is not None:
        return float(explicit), "caller"
    env = os.getenv("SPENDGUARD_CONFORMANCE_DEADLINE_S")
    if env:
        return float(env), "env"
    from spendguard import adapters
    in_chars = len(payload)
    worst, worst_basis = 0.0, "default"
    for m in models:
        secs, basis = adapters.deadline_for(m, intent="conformance:panel-review", in_chars=in_chars,
                                            default_s=_PANEL_DEADLINE_DEFAULT_S)
        secs = secs or _PANEL_DEADLINE_DEFAULT_S
        if secs > worst:
            worst, worst_basis = secs, f"measured:{m}({basis})"
    return (worst or _PANEL_DEADLINE_DEFAULT_S), worst_basis


def estimate_panel(models=None, budget_usd=2.0, est_out_tok=3200):
    """Zero-spend $ projection for the panel (one whole-payload review per model). {rows, total_usd, within_budget,
    unpriced}. est_out_tok is reasoning-INCLUSIVE (a thoughtful review thinks). Same completeness contract as the suite
    estimate: an unpriced reviewer makes the total INDETERMINATE (not silently free), so within_budget reads false."""
    from spendguard import pricing
    models = models or panel_models()
    in_tok = _rough_tokens(build_payload())
    rows, total, unpriced = [], 0.0, []
    for m in models:
        bare = m.split(":", 1)[-1] if ":" in m else m
        try:
            per = pricing.realtime_cost(bare, in_tok, est_out_tok)
        except Exception:
            per = None
        if per is None:
            unpriced.append(m)
        else:
            total += per
        rows.append({"model": m, "in_tok": in_tok, "out_tok": est_out_tok, "usd": per})
    within = (not unpriced) and (total <= budget_usd)
    return {"rows": rows, "in_tok": in_tok, "total_usd": round(total, 3), "budget_usd": budget_usd,
            "within_budget": within, "unpriced": unpriced}


def render_estimate(est):
    out = ["Panel review — ZERO-SPEND estimate (no calls made):", "",
           "  one whole-suite review per reviewer · input ~%d tok · est reasoning-inclusive out %d tok"
           % (est["in_tok"], est["rows"][0]["out_tok"] if est["rows"] else 0), ""]
    for r in est["rows"]:
        u = "—(unpriced)" if r["usd"] is None else ("$%.4f" % r["usd"])
        out.append("  %-28s %10s" % (r["model"], u))
    out.append("")
    verdict = ("WITHIN + complete" if est["within_budget"]
               else ("OVER budget" if not est["unpriced"] else "INDETERMINATE (unpriced reviewers)"))
    out.append("  PROJECTED REAL $ (metered panel): $%.3f  vs budget $%.2f  →  %s"
               % (est["total_usd"], est["budget_usd"], verdict))
    if est["unpriced"]:
        out.append("  ⚠ UNPRICED reviewers (resolve or drop before running): %s" % ", ".join(est["unpriced"]))
    return "\n".join(out)


def run_panel(models=None, budget_usd=2.0, deadline_s=None):
    """Fan the WHOLE payload to each reviewer via the metered_only cross-vendor panel (each vendor answers as itself),
    collect the critiques, write them durably. Returns the run dict. APPROVAL-GATED: caller confirms the estimate first.

    The batch deadline is SIZED FROM MEASURED per-model latency (resolve_panel_deadline → adapters.deadline_for),
    never a hardcode — one deadline bounds the whole batch, so it must fit the SLOWEST reviewer or a reasoning model is
    cut mid-thought (billed reasoning tokens, no output). Explicit deadline_s or $SPENDGUARD_CONFORMANCE_DEADLINE_S wins."""
    from spendguard import lane_balance
    models = models or panel_models()
    payload = build_payload()
    deadline_s, deadline_basis = resolve_panel_deadline(models, payload, deadline_s)
    tasks = [{"model": m} for m in models]                 # one task per reviewer; same payload to each
    results = lane_balance.bulk_delegate(
        tasks, "conformance:panel-review", reasoning="medium",   # a high-stakes design review — give reviewers room to think
        model_for=lambda t: t["model"], prompt_for=lambda t: payload,
        metered_only=True,                                 # each vendor answers as ITSELF — no lane collapse (dogfoods B10/B14)
        budget_usd=budget_usd,                             # guardrail D: a real cap on the review itself
        chunk_size=len(models), force=True, deadline_s=deadline_s)
    ts = time.strftime("%Y%m%d-%H%M%S")
    outdir = os.path.join(_REPO, "conformance_runs")
    os.makedirs(outdir, exist_ok=True)
    path = os.path.join(outdir, f"panel_review_{ts}.json")
    run = {"ts": ts, "models": models, "payload_chars": len(payload),
           "deadline_s": deadline_s, "deadline_basis": deadline_basis,
           "reviews": [{"model": (r or {}).get("model"), "text": (r or {}).get("text"),
                        "error": (r or {}).get("error"), "cost": (r or {}).get("cost")} for r in results]}
    with open(path, "w") as f:
        json.dump(run, f, indent=2)
    run["path"] = path
    return run


def main(argv=None):
    import spendguard
    spendguard.require()                                   # under the gate, fail closed
    argv = argv if argv is not None else sys.argv[1:]
    est = estimate_panel()
    if "--run" not in argv:
        print(render_estimate(est))
        print("\n  (this is the estimate only — add --run to execute the panel, AFTER approving the $ above)")
        return 0 if est["within_budget"] else 1
    if not est["within_budget"]:
        print(render_estimate(est))
        print("\n  REFUSED to run: estimate is not within budget / has unpriced reviewers.", file=sys.stderr)
        return 1
    run = run_panel()
    print("panel review complete → %s" % run["path"])
    print("  batch deadline: %.0fs  (basis: %s — measured latency, not a hardcode)"
          % (run.get("deadline_s", 0.0), run.get("deadline_basis", "?")))
    for rv in run["reviews"]:
        tag = "OK" if rv.get("text") and not rv.get("error") else ("ERROR: %s" % rv.get("error"))
        print("  %-28s %s  ($%s)" % (rv["model"], tag, rv.get("cost")))
    return 0


if __name__ == "__main__":
    sys.exit(main())

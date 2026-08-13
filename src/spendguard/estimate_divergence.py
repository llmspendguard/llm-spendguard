"""When a quoted price and the real bill disagree, SAY SO — loudly, and by judgement rather than by rule.

WHY THIS EXISTS. On 2026-08-12 a sweep was quoted at $22.86–$34.29 and measured at ~$380 — 17x. Every piece
needed to catch that automatically was already here and connected:

    record_estimate()  ->  calibrate.record_prediction()  ->  calibrate.pair()

The estimate was recorded. The actual was captured. `pair()` joined them. And then `report.py` PRINTED the
pair, and nothing else happened. A 17x divergence rendered as a row on a report that nobody was reading at
the moment the money went out. The loop was complete and inert: it observed, and it could not object.

So the missing piece was never measurement. It was CONSEQUENCE.

WHY THE JUDGEMENT IS AGENTIC. The obvious implementation is `if actual > estimate * 1.5: fail`, and that is
exactly the hand-picked cutoff this codebase forbids — it fires on a harmless 1.6x and sleeps through a
1.4x that was pure luck. Whether a divergence means "the estimate was not grounded" depends on things only
reading can settle: was the estimate built from a measured sample or from invented tokens; is the model a
reasoning model whose hidden thinking the estimate never accounted for; did the job scope change after the
quote; is the gap explained by a retry the estimate deliberately excluded. Two reasonable people can
disagree about a given pair, which is the definition of a judgement, so a model makes it.

The code's only jobs are to find every pair, hand each one over, and REFUSE when the judgement says the
estimate was not grounded. Finding and refusing are mechanical. Deciding is not.
"""
import json
import re


class EstimateNotGrounded(Exception):
    """Raised when a recorded estimate diverged from the actual for a reason a reviewer called unsound.

    An exception, not a log line. The whole failure this module addresses is an observation that produced no
    consequence, so the one thing it must not do is add another quiet record."""


JUDGE_SYS = (
    "You review cost estimates against what was actually billed, for a tool whose entire purpose is that a "
    "quoted price can be trusted. You are deciding ONE thing: was the ESTIMATE GROUNDED — built from "
    "measured evidence — or was it asserted from numbers nobody measured?\n\n"
    "A divergence is not automatically a fault. An estimate can be sound and still be wrong: the job scope "
    "changed, a retry fired that the quote deliberately excluded, a provider changed rates. Equally, a "
    "SMALL divergence can come from an ungrounded estimate that got lucky, and that is still a defect — the "
    "next one will not be lucky.\n\n"
    "Judge the METHOD, not the size of the gap. Known ways an estimate becomes ungrounded: token counts "
    "written as literals rather than measured from a sample; ignoring that a reasoning model bills hidden "
    "thinking as output; projecting from a sample taken at different settings than the run; assuming a "
    "cache discount a provider does not give.")

JUDGE_SCHEMA = '{"grounded": true|false, "why": "...", "what_to_fix": "..."}'


def _pairs():
    """Every (estimate, actual) the calibrator has joined. Reading a recorded join, not re-deriving it."""
    from . import calibrate
    try:
        return list(calibrate.pair() or [])
    except Exception as e:
        raise EstimateNotGrounded(
            f"cannot read estimate/actual pairs ({e}) — a spend guard that cannot check its own quotes "
            f"against the bill is not guarding anything. Fix the pairing before trusting any estimate.")


def adjudicate_grounding(pair, model=None):
    """Was THIS estimate grounded? Agentic — the answer depends on how the number was produced."""
    from . import adapters, calls, config, output_contract
    model = model or config.advisor_model()
    body = json.dumps({k: v for k, v in dict(pair).items() if k not in ("prompt", "output")}, default=str)
    with calls.context(intent="spendguard:estimate-divergence"):
        r = adapters.call(model,
                          f"An estimate and the actual bill for the same job:\n\n{body[:2000]}\n\n"
                          f"Was the estimate GROUNDED in measurement?\nReply JSON only: {JUDGE_SCHEMA}",
                          sig="probe:estimate-divergence", system=JUDGE_SYS, reasoning="minimal")
    if r.get("error") or not r.get("text"):
        # UNJUDGED IS NOT CLEARED. A verdict we could not obtain must not read as approval — that is the
        # same collapse (unverified == clean) this codebase keeps finding elsewhere.
        return {"grounded": None, "why": f"no verdict ({r.get('error') or 'no text'})", "what_to_fix": ""}
    obj, _ = output_contract._as_obj(r["text"])
    return {"grounded": obj.get("grounded"), "why": obj.get("why", ""),
            "what_to_fix": obj.get("what_to_fix", "")}


def enforce(raise_on_fail=True, model=None):
    """Judge every recorded estimate against its actual. Returns the verdicts; RAISES on an ungrounded one.

    Call this wherever an estimate authorizes spending, and in CI. `raise_on_fail=False` is for reporting
    surfaces that must render rather than throw — but the default is to raise, because the default failure
    of this whole area has been observing without objecting."""
    out = []
    for p in _pairs():
        v = adjudicate_grounding(p, model=model)
        v["pair"] = p
        out.append(v)
    bad = [v for v in out if v.get("grounded") is False]
    unjudged = [v for v in out if v.get("grounded") is None]
    if bad and raise_on_fail:
        lines = "\n".join(f"  - {v['why']}  FIX: {v['what_to_fix']}" for v in bad[:5])
        raise EstimateNotGrounded(
            f"{len(bad)} recorded estimate(s) were judged NOT GROUNDED in measurement:\n{lines}\n"
            f"An estimate that was not measured is a guess with a dollar sign on it. Rebuild it from a real "
            f"sample (spendguard.estimate.fit_from_sample) before quoting a price from it.")
    return {"judged": out, "ungrounded": bad, "unjudged": unjudged}


def cmd(argv=None):
    """`spendguard estimate-divergence` — judge every recorded quote against the bill."""
    import sys
    argv = list(sys.argv[2:] if argv is None else argv)
    try:
        res = enforce(raise_on_fail=False)
    except EstimateNotGrounded as e:
        print(f"REFUSED: {e}")
        return 1
    n = len(res["judged"])
    print(f"{n} estimate/actual pair(s) judged · {len(res['ungrounded'])} NOT GROUNDED · "
          f"{len(res['unjudged'])} UNJUDGED (no verdict — not the same as clean)")
    for v in res["ungrounded"]:
        print(f"  NOT GROUNDED: {v['why']}\n     fix: {v['what_to_fix']}")
    return 1 if res["ungrounded"] else 0

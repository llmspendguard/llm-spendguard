"""best_value — resolve reasoning="best-value" to a (model, effort), AGENTICALLY, from the measured learnings.

A caller passes reasoning="best-value" instead of an effort ordinal to say: *you pick the model AND the reasoning
effort — meet this intent's quality at the lowest cost, from what you have MEASURED.* This delegates BOTH axes to
the learnings, each decided the right way:

  · the EFFORT is owned by effort_titration — the learned `effort:<intent>` fact per model (models.effort_for),
    itself the product of an agentic A/B verdict; None → the model's family floor.
  · the MODEL is chosen by the advisor's AGENTIC ranker (advisor.recommend_models) over the measured cost×quality
    frontier — an LLM judgement that weighs cost, quality, and evidence strength, never a hand-picked threshold.
    (This is why best_value makes no arithmetic bar/min-sample cut itself: those are meaning-judgements, and the
    reasoner makes them.)

WHY IT MAY SPEND. Choosing the model is a judgement, so it uses a small meta-caged advisor call (estimate-first,
refused over its cap) — the caller opted into best-value; the effort lookup is $0. [Follow-up: cache the per-intent
model verdict, invalidated by an evidence fingerprint, to amortize this to ~one pick per intent rather than per call.]

DIVERSITY / PINNING. pin_model=True (a caller that also set no_substitution — e.g. a cross-vendor panel member)
keeps the named model and only supplies its learned effort, so a member is titrated, never swapped — the collapse
a global "pick the best model" would cause is impossible. No measured basis → model=None and the caller keeps its
named model (the honest unresolved path). Never raises except to PROPAGATE a deliberate stop (a spend refusal).
"""
from . import advisor, gate, models


def select_model_effort(intent, requested_model, pin_model=False, quality_target=None):
    """Resolve the best-value (model, effort) for `intent`. ALWAYS returns a dict; `model` is None when there is no basis to choose
    (the caller then keeps its named model). `considered` records how the choice was made (source + candidate
    count + the advisor's note) so a recommendation is never a black box."""
    considered = {"source": None, "model_candidates": 0, "note": None}

    def _none(why):
        return dict(model=None, effort=None, why=why, considered=considered)

    if not intent:
        return _none("best-value: no intent in context — keep the named model")

    if pin_model:
        # PANEL / pinned: never swap the model (diversity); supply the learned effort for THIS model, if measured.
        eff = models.effort_for(requested_model, intent)
        considered["source"] = "effort-fact"
        if not eff:
            return _none("best-value: no learned effort for the pinned model on '%s' — family floor stands" % intent)
        return dict(model=requested_model, effort=eff, considered=considered,
                    why="best-value: pinned %s @ learned effort %s (titration)" % (requested_model, eff))

    # CROSS-MODEL: the MODEL is an AGENTIC judgement over the measured frontier (recommend_models — meta-caged,
    # estimate-first). A deliberate stop (a spend refusal / deadline) PROPAGATES; any other advisor hiccup degrades
    # honestly to the named model. The reasoner ranks only models with evidence, so a cold intent yields no pick.
    try:
        rec = advisor.recommend_models(intent, k=1, quality_bar=quality_target, run=True)
    except Exception as e:
        if isinstance(e, gate.deliberate_stop_types()):
            raise
        return _none("best-value: advisor unavailable (%s) — keep the named model" % type(e).__name__)
    top = (rec or {}).get("top") or []
    considered["source"] = "advisor.recommend_models"
    considered["model_candidates"] = (rec or {}).get("ranked_from") or len(top)
    considered["note"] = (rec or {}).get("note")
    if not top or not top[0].get("id"):
        return _none("best-value: %s" % ((rec or {}).get("note") or "advisor produced no pick — run a bakeoff first"))
    chosen = top[0]["id"]
    eff = models.effort_for(chosen, intent)                    # effort from the titration fact (None → family floor)
    return dict(model=chosen, effort=eff, considered=considered,
                why="best-value: %s%s — %s" % (chosen, ("@" + eff) if eff else "",
                    top[0].get("why") or "advisor's cheapest that meets the intent's measured quality"))

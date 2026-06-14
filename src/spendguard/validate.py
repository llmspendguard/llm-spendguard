"""Layer 2 — LIVING insights: re-validate learnings as the corpus grows.

A learning recorded once is a guess; a learning re-checked against new data is knowledge. This pass
re-tests each insight against the CURRENT corpus and moves it through its lifecycle:
  - the cost gap it's built on still holds (cheaper-cited model still cheaper) → support++, confidence up,
    candidate → active once corroborated twice;
  - a cited model vanished / the gap inverted → contradiction++, confidence decayed, → refuted/superseded;
  - nothing checkable (free-form lesson, no two priced models) → last_validated refreshed, status untouched.
Deterministic + zero spend (a caged LLM adjudication of free-form insights can layer on later). The point:
the advisor weights by CURRENT confidence/status, so stale advice sinks and corroborated advice rises.

CLI: `spendguard validate`.
"""
import re
from . import learn, calls, pricing


def _known_models():
    models = set()
    for _prov, ms in pricing.providers().items():
        models |= set(ms)
    for _i, m, *_ in calls.summary():
        if m:
            models.add(m)
    return models


def _per_job():
    agg = {}
    for _i, m, jobs, cost, _g, _b in calls.summary():
        a = agg.setdefault(m, [0, 0.0])
        a[0] += jobs or 0
        a[1] += cost or 0
    return {m: (c / j) for m, (j, c) in agg.items() if j}


def _models_in(text, known):
    """Models cited as whole tokens — NOT substrings (so 'gpt-5' doesn't match inside 'gpt-5.5')."""
    t = (text or "").lower()
    found = []
    for m in known:
        ml = m.lower()
        # boundary = a model id is [a-z0-9.\-]; the match must not be flanked by more of those chars
        if re.search(r"(?<![a-z0-9.\-])" + re.escape(ml) + r"(?![a-z0-9.\-])", t):
            found.append(m)
    return found


def _recheck(ins, known, perjob, present):
    """Return ('support'|'contradict'|'superseded'|'unknown', note)."""
    text = " ".join(str(ins.get(k) or "") for k in ("lesson", "action", "condition", "evidence"))
    cited = _models_in(text, known)
    if not cited:
        return "unknown", "no model cited"
    gone = [m for m in cited if m not in present]
    if gone:
        return "superseded", f"cited model absent from corpus: {gone[0]}"
    priced = [(m, perjob[m]) for m in cited if m in perjob]
    if len(priced) >= 2:
        priced.sort(key=lambda x: x[1])
        cheapest, costliest = priced[0], priced[-1]
        # the insight is built on a cost gap; it holds if the gap is still real
        if costliest[1] > cheapest[1] * 1.2:
            return "support", f"{cheapest[0]} still ~{costliest[1]/cheapest[1]:.0f}x cheaper than {costliest[0]}"
        return "contradict", f"cost gap among {[m for m,_ in priced]} has collapsed"
    return "support", "cited models still present"


def _apply(ins, verdict):
    """Lifecycle transition. Returns the fields to update (or None)."""
    conf = ins.get("confidence") or 0.5
    support = ins.get("support") or 0.0
    contra = ins.get("contradiction") or 0.0
    status = ins.get("status") or "candidate"
    ver = ins.get("version") or 1
    if verdict == "support":
        support += 1
        conf = min(0.95, conf + 0.05)
        if status == "candidate" and support >= 2:
            status, ver = "active", ver + 1
    elif verdict == "contradict":
        contra += 1
        conf *= 0.6
        if contra >= 2 or conf < 0.3:
            status, ver = "refuted", ver + 1
    elif verdict == "superseded":
        status, ver = "superseded", ver + 1
        conf *= 0.7
    return dict(confidence=round(conf, 3), support=support, contradiction=contra,
                status=status, version=ver, last_validated=learn._now())


def validate(verbose=True):
    known = _known_models()
    perjob = _per_job()
    present = set(perjob)
    rows = learn.insights_full(include_refuted=True)
    counts = {"support": 0, "contradict": 0, "superseded": 0, "unknown": 0}
    promoted, refuted = 0, 0
    for ins in rows:
        verdict, note = _recheck(ins, known, perjob, present)
        counts[verdict] += 1
        before = ins.get("status")
        fields = _apply(ins, verdict)
        learn.update_insight(ins["id"], **fields)
        if fields["status"] == "active" and before != "active":
            promoted += 1
        if fields["status"] in ("refuted", "superseded") and before not in ("refuted", "superseded"):
            refuted += 1
            if verbose:
                print(f"  ↓ {fields['status']}: {ins['lesson'][:80]}  ({note})")
    print(f"validate — {len(rows)} insights re-checked: "
          f"{counts['support']} corroborated, {counts['contradict']} contradicted, "
          f"{counts['superseded']} superseded, {counts['unknown']} unverifiable.")
    print(f"  → {promoted} promoted candidate→active; {refuted} demoted (refuted/superseded). "
          f"Advisor now weights by current confidence + status.")
    # show the current top active insights
    act = [i for i in learn.insights_full() if (i.get("status") == "active")]
    if act:
        print("  top active learnings:")
        for i in act[:6]:
            print(f"    [{i['confidence']:.2f}] {i['lesson'][:90]}")
    return counts


def main(argv=None):
    return 0 if validate() is not None else 1

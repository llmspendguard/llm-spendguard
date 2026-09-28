"""Guards for the intent-alignment violations fixed 2026-09-28 ("get to honest") — the repo betraying its OWN stated
INTENT.md invariants. Each fix here is locked so it cannot silently regress:

  #1 (meaning is agentic) — conv.realtime_token_tally attached a model to a usage block via first-substring-wins, a
      GUESS when a window named 2+ models. Now: a SINGLE family match is a fixed-convention parse (used); 2+ is REFUSED
      and surfaced as skipped_ambiguous, never guessed.
  #2 (unknown price fails loud) — gate._rt_precheck caught the KeyError realtime_cost raises for an unpriced/ambiguous
      model and SILENTLY defaulted the estimate to $0 (skipping the budget precheck for exactly that model). Now: it
      WARNS loudly (the call still proceeds — advisory precheck; actual cost recorded post-call), never a silent $0;
      a DELIBERATE spend refusal from pricing PROPAGATES.
  #3 (task→intent mapping is a clear parse or a fresh slug — never a guess) — brief._match_intent mapped a task to an
      existing intent by word-overlap and, on a TIE (2+ intents equally overlapping), took the first — a guess between
      equals. Now: a tie is REFUSED (returns no match → the caller mints a new slug), only a UNIQUE best match is used.

Offline + deterministic ($0). Isolation: SPENDGUARD_HOME → mkdtemp before importing spendguard.
"""
import os, sys, tempfile, warnings

os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="spendguard-intentfix-")

from spendguard import resources, conv, gate, brief   # noqa: E402


class Checks:
    def __init__(self):
        self.fails = 0

    def ck(self, label, cond, extra=""):
        if not cond:
            self.fails += 1
        print(f"  [{'OK' if cond else 'FAIL'}] {label}{('  — ' + extra) if extra and not cond else ''}")


def main():
    c = Checks()

    # ── #1: model attribution is a fixed-convention PARSE or an honest SKIP — never a first-hit-wins GUESS ──
    c.ck("single model in a window → one match (a fixed-convention parse)",
         resources._family_matches("=== usage === claude-opus ran") == ["claude-opus-4-8"])
    c.ck("two models near one usage block → 2 matches (ambiguous → caller must refuse, not guess)",
         len(resources._family_matches("opus vs gpt-5 comparison")) == 2)
    c.ck("no model named → empty (honest skip)", resources._family_matches("nothing here") == [])

    # realtime_token_tally REFUSES the ambiguous window (surfaced as skipped_ambiguous, not mislinked to the first)
    d = tempfile.mkdtemp(prefix="rt-tally-")
    with open(os.path.join(d, "sess1.jsonl"), "w") as fh:
        fh.write("switched from opus to gpt-5 here === USAGE === 5000 in / 400 out done\n")   # AMBIGUOUS window
        fh.write("neutral filler line with no model mentioned " * 8 + "\n")                   # >120c gap so windows don't overlap
        fh.write("plain claude-opus call === USAGE === 3000 in / 200 out\n")                  # UNAMBIGUOUS → counted
    r = conv.realtime_token_tally(d)
    c.ck("realtime_token_tally surfaces skipped_ambiguous (never guesses the model on a 2-model window)",
         r.get("skipped_ambiguous", 0) >= 1, str(r))
    c.ck("the UNAMBIGUOUS usage block is still counted", r.get("calls", 0) >= 1, str(r))

    # ── #2: an unpriced/ambiguous model in the realtime precheck WARNS — never a silent $0. The path is isolated
    #        (realtime_cost raises, _rt_precheck_usd is a no-op), so ANY warning surfaced is the unpriced-precheck fix. ──
    real_cost, real_usd = gate.pricing.realtime_cost, gate._rt_precheck_usd
    gate.pricing.realtime_cost = lambda *a, **k: (_ for _ in ()).throw(KeyError("no price for 'mystery-model'"))
    gate._rt_precheck_usd = lambda *a, **k: None
    try:
        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            gate._rt_precheck("openai", "mystery-model", 1000, 500)   # must NOT raise, MUST surface a warning
        c.ck("unpriced model in the precheck surfaces a warning (not a silent $0)", len(w) >= 1,
             "no warning captured")
    finally:
        gate.pricing.realtime_cost, gate._rt_precheck_usd = real_cost, real_usd

    # a DELIBERATE stop from pricing still PROPAGATES out of the precheck (never downgraded to a $0 estimate)
    gate.pricing.realtime_cost = lambda *a, **k: (_ for _ in ()).throw(gate.SpendGateRefused("budget refused"))
    raised = False
    try:
        gate._rt_precheck("openai", "m", 1, 1)
    except gate.SpendGateRefused:
        raised = True
    finally:
        gate.pricing.realtime_cost = real_cost
    c.ck("a deliberate spend refusal from pricing PROPAGATES (never a silent $0)", raised)

    # ── #3: brief._match_intent maps a task to an intent by a clear (unique) word-overlap or REFUSES — never guesses a tie ──
    brief._known_intents = lambda: ["loinc typing codes", "loinc typing labels", "invoice reconcile"]
    m_tie, known_tie = brief._match_intent("loinc typing")            # ties the first two (overlap 2 each) → REFUSE
    c.ck("brief._match_intent REFUSES an ambiguous tie (2 intents overlap equally → no first-hit-wins guess)",
         m_tie is None and known_tie is False, f"got {(m_tie, known_tie)}")
    m_one, known_one = brief._match_intent("invoice reconcile now")   # unique best match → USE it
    c.ck("brief._match_intent uses a UNIQUE best match", m_one == "invoice reconcile" and known_one is True,
         f"got {(m_one, known_one)}")
    m_none, known_none = brief._match_intent("xyzzy plugh")           # overlaps nothing → no match (caller mints a slug)
    c.ck("brief._match_intent returns no match when nothing overlaps (fresh slug)",
         m_none is None and known_none is False, f"got {(m_none, known_none)}")

    print(f"\n{'[FAIL]' if c.fails else 'OK'} test_intent_violation_fixes: {c.fails} failure(s)")
    return 1 if c.fails else 0


if __name__ == "__main__":
    sys.exit(main())

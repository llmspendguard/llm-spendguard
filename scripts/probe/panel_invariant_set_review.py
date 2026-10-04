"""PROMPT 9 made real: an ADVERSARIAL cross-vendor completeness review of the 429-storm invariant->test MAP, BEFORE
more code is built on it. Feeds the panel the system model + the map + the actual unit test WHOLE (no truncation — a
completeness/proxy judgement must see everything it judges) and asks each model, independently, the three Prompt 9
questions: what invariant is MISSING for a system of this class, which tests prove a PROXY not the invariant, and which
DEFERRALS (⏳) are unsafe. Routes $0 via the subscription lanes where a model is served, else metered (tiny).

Usage:
  python scripts/probe/panel_invariant_set_review.py --plan   # $0 estimate (token count, no calls)
  python scripts/probe/panel_invariant_set_review.py --run    # run the panel
"""
import argparse
import os
import sys

_REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(_REPO, "src"))
import spendguard  # noqa: E402
spendguard.require()
from spendguard import adapters, calls  # noqa: E402

INTENT = "design-review:invariant-set"
PANEL = ["claude-opus-4-8", "gpt-5.6-sol", "gemini-3-pro", "kimi-for-coding", "glm-5.3"]

# the artifacts the judgement consumes — read WHOLE at runtime (never a slice): the system model + map live in the
# plan doc; the unit test is the ground truth of what each invariant's test ACTUALLY asserts (for proxy detection).
ARTIFACTS = [
    ("docs/PLAN_429_storm_to_batch.md", "SYSTEM MODEL + INVARIANT→TEST MAP"),
    ("tests/test_storm_coalescer.py", "THE UNIT TEST (what each invariant's test actually asserts)"),
    ("src/spendguard/storm_coalescer.py", "THE COALESCER (the design, realized)"),
]

SYSTEM = (
    "You are a principal distributed-systems architect doing an ADVERSARIAL completeness review of an invariant→test "
    "map for a CONCURRENT LLM-submission governor, BEFORE further code is built on it. The map was written by ONE mind "
    "and therefore has blind spots; your job is to find what is MISSING, not to praise what is present. Be concrete: "
    "name each missing invariant as a testable property, not 'looks thorough'.")

QUESTIONS = """\
Answer THREE questions about the invariant→test map in the artifacts below.

1. COMPLETENESS — For a system of THIS class (a submission governor that must take N requests of 1..thousands through
   ONE async door, pace realtime under the rate wall, divert overflow to a Batch API, and return every result with
   ZERO surfaced 429s), what INVARIANT or FAILURE MODE is MISSING from the map? Check it against EACH standard axis and
   name concretely any gap: conservation; ordering/demux; rate safety (incl. multi-window / per-second sub-limits);
   idempotency & crash-resume (no double-spend, no dropped request); cross-PROCESS shared cap (multiple processes, one
   vendor limit); deadline → typed backpressure (never a raw 429); the Batch API's OWN limits (don't move the storm to
   batch); security (e.g. a poisoned request in a cohort); economics (prefer $0 lane / batch price when it satisfies);
   observability/ledger evidence. For each missing item: a one-line NAME + the ASSERTION a test would make + the LAYER.

2. PROXY-TEST — For each row that HAS a test, does the named test (see the unit test source) actually PROVE that
   invariant, or a cheaper PROXY? Call out any assertion that would still pass if the real behavior were absent.

3. DEFERRAL SAFETY — For each DEFERRED (⏳) row (I10 one-chokepoint wiring, I13 idempotency/SIGKILL, cross-process), is
   deferring it safe RIGHT NOW, or does the contract ("always success, always returned, zero surfaced 429, for 1 or
   1000s, all providers") silently break without it? Say which deferrals must become blockers.

End with the SINGLE most important missing invariant — the one whose absence would most likely cause a real incident.
"""


def _load_artifacts():
    chunks = []
    for rel, label in ARTIFACTS:
        p = os.path.join(_REPO, rel)
        with open(p, "r") as fh:
            body = fh.read()                                  # WHOLE file — no slice
        chunks.append("===== %s  (%s) =====\n%s" % (label, rel, body))
    return "\n\n".join(chunks)


def _emit(out_fh, text):
    """Print AND durably append+flush — so a pipe/buffer/truncation can never lose a paid response again."""
    print(text, flush=True)
    if out_fh is not None:
        out_fh.write(text + "\n")
        out_fh.flush()
        os.fsync(out_fh.fileno())


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", action="store_true")
    ap.add_argument("--plan", action="store_true")
    ap.add_argument("--out", default=None, help="durable results file (each model block appended+fsynced as it returns)")
    a = ap.parse_args()
    artifacts = _load_artifacts()
    prompt = QUESTIONS + "\n\n" + artifacts
    approx_tok = len(prompt) // 4
    print("== invariant-set adversarial panel ==  models=%s  prompt~%d tok (artifacts sent WHOLE)" % (PANEL, approx_tok))
    if a.plan or not a.run:
        print("--plan: $0 (no calls). Re-run with --run to execute the panel ($0 on lanes where served).")
        return
    out_fh = open(a.out, "w") if a.out else None
    try:
        for m in PANEL:
            with calls.context(intent=INTENT, chain="panel-invariant-set-review"):
                r = adapters.call(m, prompt, system=SYSTEM, reasoning="high", no_substitution=True, timeout_s=300)
            header = "\n" + "=" * 90 + "\n### %s\n" % m + "=" * 90
            if isinstance(r, dict) and r.get("text"):
                _emit(out_fh, header + "\n  [executor=%s billed=$%s]\n" % (r.get("executor"), r.get("cost")) + r["text"].strip())
            else:
                _emit(out_fh, header + "\n  (no text: %s)" % (r.get("error") if isinstance(r, dict) else r))
    finally:
        if out_fh is not None:
            out_fh.close()


if __name__ == "__main__":
    main()

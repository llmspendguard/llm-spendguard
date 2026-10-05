"""Ask a cross-vendor LLM panel whether the 429-storm TEST actually proves the user's INTENT — the user explicitly
asked "ask other llms if what you did actually meets the intent." Routes $0 via the subscription lanes. Adversarial:
assume the test is too shallow and find exactly where it fails to prove the end-to-end guarantee.

Usage: python scripts/probe/review_test_meets_intent.py --run
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "src"))
import spendguard  # noqa: E402
spendguard.require()
from spendguard import adapters, calls, model_catalog  # noqa: E402

INTENT = "design-review:test-meets-intent"


def _panel():
    out = []
    for fam in ("opus", "sonnet"):
        ids = ((model_catalog.vendor_record("anthropic") or {}).get("model_family_map") or {}).get(fam) or []
        for mid in ids:
            if model_catalog.model_record(mid):
                out.append(mid)
                break
    for mid in ("gpt-5.6-sol", "gemini-3-pro"):          # cross-vendor, resolved/aliased by the adapter if needed
        out.append(mid)
    return out[:3] or ["gpt-5.6-sol"]


THE_INTENT = """\
THE USER'S INTENT (verbatim intent, their words): "if you are pushing lane, and it identifies it might be a storm,
then it has to automatically create a batch. This is a clear way to ensure actual execution to ensure that request
gets a response — for 1 or for 1000s, it does not matter, always success and always returned. The bundling and
unbundling and whatever is managed by llmspendguard — that's the whole point of the queue: the 1 sec to see, and
then return, and the async call." So: a caller submits N requests (N=1 or N=1000s) through ONE entry point;
spendguard observes (~1s coalesce) whether this will storm the realtime rate limit; if so it AUTOMATICALLY bundles
them into Batch-API jobs, runs them, UNBUNDLES the results, and returns a response for EVERY request. Always success,
always returned, zero 429s surfaced, on the lane OR metered path — the bundling/queueing/async is spendguard's job,
invisible to the caller."""

WHAT_WAS_BUILT = """\
WHAT WAS ACTUALLY BUILT so far:
- dispatch: a PROACTIVE cold-cap seed (per-vendor rpm/tpm from a catalog) so admission PACES a cold vendor instead of
  admitting unlimited; acquire() blocks/queues over-budget calls within a deadline.
- route_horizon.should_batch_fan(provider, model, n, est_in, est_out, deadline_s): returns True/False whether N would
  exceed the realtime rate budget — a DECISION FUNCTION.
- route_horizon.chunk_for_batch(provider, n): splits N into batch-API-sized chunks — a DECISION FUNCTION.
- These two functions are NOT wired into any execution path. Nothing calls them to actually CREATE a batch, run it,
  and return results. bulk_delegate was NOT changed to auto-batch on storm.
- A live run of a 60-call governed fan recorded ZERO calls in the ledger (served from cache/lane/no-op), so the
  end-to-end was never exercised."""

THE_TEST = """\
THE TEST (tests/test_dispatch_no_429_storm.py) — 13 guards, all green, 5x:
 1  once the seeded tpm budget is consumed, the next admit queues out (paced).
 2  a small in-budget admit returns immediately.
 2b should_batch_fan(n=3000) returns True; (n=5) returns False.
 2c chunk_for_batch(500000) returns chunks that sum to 500000 and each fit the batch limit.
 3/3b catalog has a rate floor; effective_limits(cold anthropic) is non-zero.
 4  learn_rate_limit / _learn_success_limits set a tpm.
 5  AIMD shrink/grow move the conn cap.
 6  admit(skip_lane=True) does not set shed.
 7  _est_call_tokens accepts model+intent.
 8  dispatch.is_saturated exists.
All guards call governor primitives / decision functions directly. NONE submits N requests through a single caller
entry point and asserts N responses come back, nor that a storm-scale submission is actually auto-batched and its
results unbundled and returned."""

ASK = """\
QUESTION: Does THE TEST actually prove THE INTENT? Be adversarial and concrete. Specifically:
(a) Does the test prove that a caller submitting N requests GETS N responses (always success, always returned)? If
    not, say so plainly.
(b) Does it prove that a storm-scale submission is AUTOMATICALLY batched, executed, UNBUNDLED, and returned — i.e. the
    end-to-end behaviour — or does it only check that decision FUNCTIONS return the right values while the behaviour
    is unwired?
(c) What are the 3-6 assertions a REAL end-to-end test MUST make to prove the intent (think: submit N through the one
    entry, get N correct responses, zero surfaced 429s, auto-batch path actually taken + results unbundled, works for
    N=1 and N=1000s, idempotent/resumable)?
(d) Give a one-line verdict: does this test, as-is, justify telling the user 'the storm is fixed'? yes/no + why.
Answer concisely."""

SYSTEM = ("You are a skeptical staff engineer reviewing whether a test proves a system requirement. Do not be "
          "charitable. If the test checks parts but not the end-to-end behaviour, say so bluntly.")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", action="store_true")
    ap.add_argument("--plan", action="store_true")
    a = ap.parse_args()
    panel = _panel()
    prompt = THE_INTENT + "\n\n" + WHAT_WAS_BUILT + "\n\n" + THE_TEST + "\n\n" + ASK
    print("== test-meets-intent review ==  panel=%s  prompt~%d tok" % (panel, len(prompt) // 4))
    if a.plan or not a.run:
        print("--plan: $0. Re-run with --run."); return
    for m in panel:
        print("\n" + "=" * 78 + "\n### %s\n" % m + "=" * 78)
        with calls.context(intent=INTENT, chain="review-test-intent"):
            r = adapters.call(m, prompt, system=SYSTEM, reasoning="high", no_substitution=True, timeout_s=240)
        if isinstance(r, dict) and r.get("text"):
            print("  [executor=%s]\n" % r.get("executor") + r["text"].strip())
        else:
            print("  (no text: %s)" % (r.get("error") if isinstance(r, dict) else r))


if __name__ == "__main__":
    main()

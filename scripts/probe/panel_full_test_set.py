"""Ask a 5-model cross-vendor panel (opus-5 / gpt-5.6-sol / gemini / kimi / glm-5.3 — the models the user named) to
define the COMPLETE set of tests that must pass to prove the 429-storm requirement is ACTUALLY solved — developed,
wired, used, and tested end-to-end. The user's point: one example test is not enough; enumerate them all. Routes $0
via the subscription lanes where a model is served, else metered (tiny). Each model answers independently.

Usage: python scripts/probe/panel_full_test_set.py --run
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "src"))
import spendguard  # noqa: E402
spendguard.require()
from spendguard import adapters, calls  # noqa: E402

INTENT = "design-review:full-test-set"
# the models the user named; adapters resolves served ids / lane routing.
PANEL = ["claude-opus-4-8", "gpt-5.6-sol", "gemini-3-pro", "kimi-for-coding", "glm-5.3"]

PROMPT = """\
REQUIREMENT (what must be TRUE, not just coded): llm-spendguard is a governor in front of LLM APIs. A caller submits
N requests through ONE entry point — N may be 1 or thousands. spendguard must GUARANTEE every request gets a correct
response ("always success, always returned"), with ZERO 429s surfaced, for ALL providers (anthropic/openai/gemini/
zai/moonshot), on the realtime, lane, AND batch paths. The mechanism: observe briefly (~1s coalesce) at the one
admission door; if the submission would storm the realtime rate limit, spendguard AUTOMATICALLY bundles the work into
Batch-API jobs, runs them async, UNBUNDLES the results, and returns a response per request. Pacing, bundling,
unbundling, chunking, retries, resume are spendguard's job — invisible to the caller. Grounded in real incidents:
bulk fans drove 4,584 req/min against a 1,000-rpm cap → 8,088 anthropic 429s.

What exists today: a proactive per-vendor cold-cap seed (admission paces from call #1, all vendors) + an output-aware
token estimate + AIMD + decision functions should_batch_fan()/chunk_for_batch() that are NOT yet wired into execution
(nothing auto-creates+runs+unbundles a batch) + a shallow test that only calls those functions directly.

YOUR TASK: enumerate the COMPLETE set of tests required to prove this requirement is ACTUALLY solved — not one
example. Be exhaustive and concrete. For EACH test give: a one-line name, the exact ASSERTION(s) it makes, the layer
(unit / integration / end-to-end / live-metered / property / chaos-concurrency / regression), and whether it needs
real spend. Cover at least: N=1 and N=1000s end-to-end (N responses returned, zero surfaced 429s); the auto-batch
path is actually TAKEN and results UNBUNDLED+returned in original order; works per-provider AND cross-provider; the
one-chokepoint invariant (every realtime/lane/metered/batch call goes through the same admit/decision point — nothing
bypasses it); metered_only truly rides the metered API (measured billed>0 in the ledger); cross-PROCESS bursts;
sub-window sustained "boiled-frog" load; idempotency/resume (no double-spend, no dropped request); deadline honored or
a controlled backpressure error (never a raw 429); batch submission respects the batch API's own limits (no batch
429); cost/economics (prefers $0 lanes / batch when they satisfy the need); and the ledger-evidence tests that make
"done" measurable. Also: name the 2-3 tests MOST likely to be skipped or faked, and how to make them un-fakeable.
End with the single most important test that, if green with live evidence, would most justify saying "solved"."""

SYSTEM = ("You are a principal test architect. Be exhaustive and adversarial: assume the author will cut corners and "
          "fake coverage. Every test must have a concrete, checkable assertion. Prefer end-to-end + live-evidence "
          "tests over unit checks of internal functions.")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", action="store_true")
    ap.add_argument("--plan", action="store_true")
    a = ap.parse_args()
    print("== full-test-set panel ==  models=%s  prompt~%d tok" % (PANEL, len(PROMPT) // 4))
    if a.plan or not a.run:
        print("--plan: $0. Re-run with --run."); return
    for m in PANEL:
        print("\n" + "=" * 80 + "\n### %s\n" % m + "=" * 80)
        with calls.context(intent=INTENT, chain="panel-full-test-set"):
            r = adapters.call(m, PROMPT, system=SYSTEM, reasoning="high", no_substitution=True, timeout_s=300)
        if isinstance(r, dict) and r.get("text"):
            print("  [executor=%s billed=$%s]\n" % (r.get("executor"), r.get("cost")) + r["text"].strip())
        else:
            print("  (no text: %s)" % (r.get("error") if isinstance(r, dict) else r))


if __name__ == "__main__":
    main()

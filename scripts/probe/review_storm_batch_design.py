"""Adversarial cross-LLM design review of the storm->batch fix, BEFORE building — so the design is checked by more
than one brain (the user asked: use opus-5/fable/whatever). Runs the panel across cross-vendor models; routes $0 via
the subscription lanes where they serve the model (the economically-optimal way, dogfooding spendguard). Each model
is asked to ATTACK the design: find the failure modes that would stop it being 100% reliable.

Usage:  python scripts/probe/review_storm_batch_design.py --plan     # $0 estimate (counts, which lane/meter)
        python scripts/probe/review_storm_batch_design.py --run      # ask the panel (lane=$0; meter only if a lane is down)
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "src"))
import spendguard  # noqa: E402
spendguard.require()
from spendguard import adapters, calls  # noqa: E402

INTENT = "design-review:storm-to-batch"

# Cross-vendor panel of STRONG models — each rides its $0 subscription lane where available (claude-code / codex /
# gemini), metered only if that lane is down. Pinned (no_substitution) so each vendor's own view is heard.
PANEL = ["claude-opus-4-8", "gpt-5.6-sol", "gemini-3-pro"]

DESIGN = """\
CONTEXT (grounded in llm-spendguard's own ledger): bulk concurrent FANS submit THOUSANDS of batchable LLM requests at
once to a realtime rate-limited API. Measured storms: anthropic opus, Start-tier limit 1,000 requests/min, but a fan
drove 4,584 requests/MINUTE -> 3,024 HTTP 429s in one burst (repeated across days, 8,088 anthropic 429s all-time).
spendguard is a governor in front of the LLM APIs. It CANNOT see the caller's code (could be a ThreadPoolExecutor, a
loop, or many processes), so it must DETECT a storm by OBSERVATION, not by the caller declaring "I am a fan".

PROPOSED FIX ("storm -> batch"):
1. COALESCING/OBSERVATION WINDOW at the ONE universal admission door (dispatch.admit, which every call already passes
   through): the first arrival for a (vendor, model) opens a brief window (~1s, configurable); arrivals within it are
   collected. The window RELEASES EARLY when quiet (a lone call waits ~100ms), and only stays open to observe when
   arrivals cluster. At window close the governor knows N (count) and the summed input+expected-output tokens.
2. SCALE-GATE: compare the coalesced group's realtime drain to the vendor's published rate budget over its reset
   window (requests/min and tokens/min, from a catalog of real published limits). If draining N realtime would exceed
   rpm or tpm (i.e. would storm / hit acceleration limits) AND the work is non-interactive -> divert the WHOLE group to
   the Batch API (no per-minute storm, ~half the cost, and a fan is batchable by definition). Otherwise dispatch
   realtime, PACED by the governor.
3. BACKSTOPS: a proactive cold rate cap seeded from the published-limits catalog (so admission paces from call #1, never
   "unlimited"); AIMD slow-start (TCP additive-increase / multiplicative-decrease) on the rate window; learn the real
   ceiling from provider rate-limit response headers (success AND 429); an output-aware token estimate (output tokens
   per minute is metered on ACTUAL output, so use a learned expected-output, not max_tokens); and a metered-only pin
   that NEVER sheds to a $0 subscription lane under throttle (so a caller pinning the billing path keeps it).

GOAL: a caller NEVER sees a 429 storm, and spendguard stays economically optimal (prefer $0 lanes / batch where they
satisfy the need).

YOUR TASK (be adversarial — assume this is my 10th attempt and it still has a hole): find the FAILURE MODES that would
stop this being 100% reliable. Consider at least: a legitimate need for many realtime results IN-SESSION (batch is
async); heterogeneous models in one fan; cross-PROCESS bursts (separate processes don't share in-process window state);
the window adding latency to a latency-critical call; a fan smaller than the window but still over budget over several
windows; batch latency/limits; streaming calls; how to set the window size without a magic number; and whether
observing arrival-rate at admit is even the right layer. Then: is there a FUNDAMENTALLY better or simpler approach?
Answer concisely with (a) the top 3-5 failure modes ranked, (b) concrete fixes, (c) a better approach if one exists."""

SYSTEM = ("You are a rigorous distributed-systems reviewer. Attack the design for holes; do not praise it. Be concrete "
          "and concise. The author has gotten this wrong repeatedly, so prioritize the failure mode most likely to be "
          "overlooked.")


def _panel_cost_note():
    for m in PANEL:
        prov = adapters.provider_for(m)
        lane = adapters._lane_for(prov) if hasattr(adapters, "_lane_for") else None
        print("  %-20s provider=%-10s lane=%s" % (m, prov, lane or "(metered)"))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--plan", action="store_true", help="$0: show the panel + routing, no calls")
    ap.add_argument("--run", action="store_true", help="ask the panel (lane=$0 where available)")
    a = ap.parse_args()
    if not (a.plan or a.run):
        ap.error("pass --plan or --run")
    print("== storm->batch design review ==\n  panel (routes $0 via each lane where available, metered only if down):")
    _panel_cost_note()
    print("  prompt tokens ~= %d" % (len((SYSTEM + DESIGN)) // 4))
    if a.plan:
        print("\n--plan only: no calls made ($0). Re-run with --run to ask the panel.")
        return
    for m in PANEL:
        print("\n" + "=" * 78 + "\n### %s\n" % m + "=" * 78)
        with calls.context(intent=INTENT, chain="design-review-storm-batch"):
            r = adapters.call(m, DESIGN, system=SYSTEM, reasoning="high", no_substitution=True, timeout_s=240)
        if not isinstance(r, dict):
            print("  (unexpected non-dict result)"); continue
        if r.get("error"):
            print("  ERROR: %s" % str(r.get("error"))[:300]); continue
        print("  [executor=%s billed=$%s]\n" % (r.get("executor"), r.get("cost")))
        print((r.get("text") or "(no text)").strip())


if __name__ == "__main__":
    main()

"""THE ⭐ acceptance test (REQUIRED_TESTS_429_storm.md): replay the incident shape through a provider that genuinely
429s a burst, with a CONTROL ARM that proves the harness has teeth. Offline + $0 (FakeProvider), scaled down for
speed; the live 4,584-vs-1,000 variant is a separate live-metered test.

CONTROL ARM  — drive the FakeProvider directly, UNGOVERNED, with a burst. It MUST 429 (E0.3 teeth): if the simulator
               can't bleed, no "zero 429" claim below means anything.
GUARDED ARM  — submit the SAME burst through spendguard's ONE governed fan entry (bulk_delegate) with an ASYNC
               urgency horizon that sustained realtime cannot drain (the real incident: 4,584 arrivals cannot drain
               under a 1,000-rpm wall within the horizon without the backlog growing unboundedly — the condition that
               produced 8,088 429s). The correct outcome is the COMBO: realtime carries the share it can SUSTAIN and
               batch carries the OVERFLOW — not batch-only (needlessly delays what realtime could serve now) and not
               pacing-only (unbounded backlog = the storm). Assert, via the caller-side collector + the provider's own
               counters (never spendguard's own logs):
                 (1) metered_only actually reached the METERED provider (fp saw the calls) — NOT silently lane-served
                     (the lmm "metered_only did not hold" complaint, as an assertion);
                 (2) ZERO surfaced 429s;
                 (3) N submitted -> N returned (conservation), served asynchronously through the same handle;
                 (4) canary bijection (right answer for the right request, after unbundling across paths);
                 (5) PACING did its share but NO MORE — realtime served is >0 and <= the sustainable budget (an
                     all-to-batch solution fails the lower bound; a pacing-only solution fails the upper bound);
                 (6) BATCH carried the overflow — batch served >= (N - budget) across >=1 real batch job.
WHY THROUGHPUT PRESSURE, NOT A SYNCHRONOUS DEADLINE (a weakness this test was rewritten to remove): the caller is
ASYNC — it awaits N results through one handle and never assumes synchronous completion, so the trigger is not "finish
within 3s" but "sustained arrivals exceed what realtime can drain within the urgency horizon." With a GENEROUS horizon,
pacing alone legitimately yields zero-429 + N->N and batch is correctly NOT needed (panel A7 vs B2) — so a bare "batch
taken" assertion would be VACUOUS (red could mean "unwired" OR "not needed"). Under throughput pressure, pacing-only
PROVABLY cannot keep up without an unbounded backlog, so (5)/(6) RED means exactly one thing: the pace+batch COMBO the
incident requires is unwired. Today (5) and (6) are RED by construction. This file is the contract the storm fix must
turn green — not a green that pretends.
"""
import concurrent.futures as cf
import os
import sys
import tempfile
import time

# ── incident parameters (named, derived — no magic literals) ─────────────────────────────────────────────────
# The contract is ASYNC: the caller submits N units and awaits N results through the same handle; realtime and batch
# are interchangeable execution paths UNDER the door, never the caller's concern, and no 429 ever surfaces. The batch
# trigger is THROUGHPUT, not a synchronous wall-clock SLA: the FakeProvider is the "real" provider wall that 429s a
# burst; the governor paces AT OR BELOW that wall for zero 429; but if sustained arrivals exceed what the safe realtime
# rate can drain within the urgency horizon, pacing alone can only keep up by growing the backlog unboundedly (exactly
# the 4,584/min-vs-1,000/min condition that produced 8,088 429s). So the overflow MUST divert to batch.
N = 300                                  # storm size (arrives "at once")
WALL_PER_S = 50                          # FakeProvider refuses >50 realtime calls in any 1s window (the rate limit)
WALL_WINDOW_S = 1.0
SAFE_PER_S = 40                          # governor paces metered anthropic to 40/s — safely under the 50/s wall
SAFE_RPM = SAFE_PER_S * 60               # => no 429 from pacing
URGENCY_HORIZON_S = 3.0                  # how soon results are wanted (async; a hint, NOT a synchronous block): at
REALTIME_BUDGET = int(SAFE_PER_S * URGENCY_HORIZON_S)  # 40/s the realtime path can sustain 40*3=120 within it, so the
REALTIME_SLACK = SAFE_PER_S              # other >= N-120 = 180 MUST batch. One second of slack for pacing jitter.
MODEL = "anthropic:claude-haiku-4-5"

os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="sg-replay-")
os.environ.setdefault("SPENDGUARD_TEST_ISOLATED", "1")
os.environ.setdefault("SPENDGUARD_NO_AUTOINSTALL", "1")
os.environ["SPENDGUARD_DISPATCH_MANAGE_ALL"] = "1"
os.environ["SPENDGUARD_DISPATCH_RPM_ANTHROPIC"] = str(SAFE_RPM)
_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(_HERE), "src"))
sys.path.insert(0, _HERE)

import spendguard  # noqa: E402
spendguard.require = lambda: None
import storm_harness as H  # noqa: E402
from spendguard import storm_submit  # noqa: E402

fails = []


def ck(name, cond, extra=""):
    print(("  [OK]   " if cond else "  [RED]  ") + name + (("  — " + extra) if extra and not cond else ""))
    if not cond:
        fails.append(name)


# ── CONTROL ARM: ungoverned burst must 429 (teeth) ───────────────────────────────────────────────────────────
print("-- CONTROL ARM (spendguard OFF): an ungoverned burst must make the provider 429 (else the test is vacuous) --")
ctl = H.FakeProvider(cap=WALL_PER_S, window_s=WALL_WINDOW_S)
with cf.ThreadPoolExecutor(max_workers=N) as ex:
    list(ex.map(lambda i: ctl._realtime("haiku", "do %s" % H.canary(i)), range(N)))
ck("control: the FakeProvider refused a burst with real 429s (harness has teeth)", ctl.realtime_429 > 0,
   extra="realtime_429=%d — a burst of %d past %d/%.0fs should 429" % (ctl.realtime_429, N, WALL_PER_S, WALL_WINDOW_S))

# ── GUARDED ARM: same burst through spendguard's governed storm entry, under throughput pressure ──────────────
print("\n-- GUARDED ARM (spendguard ON, via submit_storm): N=%d, wall=%d/s, safe=%d/s, horizon=%.0fs (async)"
      " => realtime budget %d, so >=%d MUST batch (the COMBO) --" % (N, WALL_PER_S, SAFE_PER_S, URGENCY_HORIZON_S,
                                                                     REALTIME_BUDGET, N - REALTIME_BUDGET))
fp = H.FakeProvider(cap=WALL_PER_S, window_s=WALL_WINDOW_S).install()      # realtime egress → the fake wall
col = H.CallerCollector()
submitted = [H.canary(i) for i in range(N)]
_raw_model = MODEL.split(":", 1)[1]


def _exec_batch(items):
    """The provider-aware batch executor submit_storm requires — FakeProvider-backed for this offline test. Id-keyed:
    items=[(custom_id, prompt)] -> {custom_id: result} (the real Batch-API shape)."""
    return fp.batch_submit([(cid, _raw_model, prompt) for cid, prompt in items])


t0 = time.monotonic()
try:
    res = storm_submit.submit_storm(
        [{"i": i} for i in range(N)], intent="acceptance:incident-replay", model=MODEL,
        execute_batch=_exec_batch, deadline_s=URGENCY_HORIZON_S, system="terse", reasoning="minimal",
        prompt_for=lambda t: "do %s" % H.canary(t["i"]), max_workers=16)
    for r in (res or []):
        col.record(r)
finally:
    fp.uninstall()
elapsed = time.monotonic() - t0

served = fp.realtime_calls + fp.batch_calls
ck("1. metered_only actually reached the METERED provider (not silently lane-served)", served > 0,
   extra="fp saw realtime=%d batch=%d — 0 means metered_only rode a $0 lane (defect #3)" % (fp.realtime_calls, fp.batch_calls))
ck("2. ZERO surfaced 429s to the caller", col.surfaced_429 == 0, extra="surfaced_429=%d" % col.surfaced_429)
ok_results = sum(1 for r in (res or []) if isinstance(r, dict) and r.get("text") and not r.get("error"))
ck("3. N submitted -> N returned (conservation)", ok_results == N, extra="returned %d/%d" % (ok_results, N))
ck("4. canary bijection (right answer for the right request)", col.bijection_ok(submitted),
   extra="returned canaries != submitted set")
ck("5. PACING did its share but NO MORE — realtime in (0, sustainable budget] (the COMBO, not all-batch/all-realtime)",
   0 < fp.realtime_calls <= REALTIME_BUDGET + REALTIME_SLACK,
   extra="realtime_calls=%d, want 0<rt<=%d(+%d slack) [ran %.1fs] — %s" % (
       fp.realtime_calls, REALTIME_BUDGET, REALTIME_SLACK, elapsed,
       "all 300 rode realtime: pacing-only, unbounded backlog = the storm" if fp.realtime_calls > REALTIME_BUDGET + REALTIME_SLACK
       else "realtime carried nothing: all-to-batch needlessly delays what realtime could serve now"))
ck("6. BATCH carried the overflow — batch served >= N-budget across >=1 real batch job",
   fp.batch_jobs > 0 and fp.batch_calls >= (N - REALTIME_BUDGET - REALTIME_SLACK),
   extra="batch_jobs=%d batch_calls=%d, want jobs>0 and calls>=%d — overflow not diverted to batch (UNWIRED today)"
         % (fp.batch_jobs, fp.batch_calls, N - REALTIME_BUDGET - REALTIME_SLACK))

print(f"\n{'ALL GREEN' if not fails else 'RED'}: test_incident_replay_storm — {len(fails)} assertion(s) RED"
      f"  [guarded arm ran {elapsed:.1f}s, realtime={fp.realtime_calls} batch={fp.batch_calls}"
      f" internal_429_absorbed={fp.realtime_429} 429(caller)={col.surfaced_429}]")
if fails:
    print("  (expected RED on 5/6 until the pace+batch COMBO is wired end-to-end — this file is the contract, not a fake green)")
sys.exit(1 if fails else 0)

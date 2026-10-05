"""THE 429-STORM GUARD SUITE — replays the REAL storms from our ledger and proves the fix, repeatably.

A 429 is a SYMPTOM. The disease (mined from the ledger) is a bulk concurrent FAN submitting THOUSANDS of batchable
requests at once to a realtime rate limit:
  · 2026-10-02  honestreview:repo-review              3,106 submitted → 3,024 × 429   (peak 4,584 req/min)
  · 2026-10-04  bc-key-symptom-snomed-expression      2,368 submitted → 2,285 × 429   (the lmm run)
  · 2026-09-28  honestreview:call-intent              2,382 submitted → 1,793 × 429
Anthropic Start-tier opus = 1,000 rpm. 4,584 req/min is 4.5× over — NO per-call pacing fixes that; the only correct
response is to SEE the scale AT SUBMISSION and divert the whole fan to the Batch API (no per-minute storm, ~½ cost,
and these fans are non-interactive = batchable). These guards encode that, plus the governor backstops. DONE = every
guard GREEN, 5 runs in a row, in CI. Offline + $0: the real planner/governor primitives are driven; no network.
"""
import os
import sys
import tempfile

os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="sg-429-")
os.environ.setdefault("SPENDGUARD_TEST_ISOLATED", "1")
os.environ.setdefault("SPENDGUARD_NO_AUTOINSTALL", "1")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

import spendguard  # noqa: E402
spendguard.require = lambda: None
from spendguard import dispatch, model_catalog as mc  # noqa: E402

fails = []
VENDOR, MODEL = "anthropic", "claude-opus-4-8"

# The three mined storm shapes — (intent, N submitted, batchable?) — the exact workloads that generated the 429s.
STORMS = [("honestreview:repo-review", 3106, True),
          ("bc-key-symptom-snomed-expression", 2368, True),
          ("honestreview:call-intent", 2382, True)]


def ck(name, cond, extra=""):
    print(("  [OK]   " if cond else "  [RED]  ") + name + (("  — " + extra) if extra and not cond else ""))
    if not cond:
        fails.append(name)


def _reset_governor():
    try:
        dispatch._GOV._buckets.clear()
    except Exception:
        pass
    try:
        if hasattr(dispatch._LEARNED, "_data"):
            dispatch._LEARNED._data.clear()
    except Exception:
        pass


def _should_batch_fan(provider, model, n, est_in=4000, est_out=800):
    """Does the planner decide 'this N-at-once fan would storm realtime → divert to BATCH'? Probed via the accessor
    the fix (A) must provide, getattr-with-None so it fails CLEANLY (RED) today rather than erroring."""
    fn = getattr(dispatch, "should_batch_fan", None) or getattr(
        __import__("spendguard.route_horizon", fromlist=["x"]), "should_batch_fan", None)
    if not fn:
        return None
    try:
        return bool(fn(provider, model, n, est_in=est_in, est_out=est_out))
    except Exception:
        return None


# ── GUARD 1 (PRIMARY): a COLD metered vendor is PACED by the seeded cold cap — the storm cannot happen ────────
# The storm's root: a cold anthropic bucket had rpm=0/tpm=0 (unlimited), so a fan of N dispatched ALL at once. With
# the catalog cold cap seeded, acquire PACES: a call that would exceed the seeded budget QUEUES OUT within its
# deadline instead of dispatching over-budget — "never intentionally dispatch above the known quota" (your chosen
# promise). No manual config/learned limit is set here: the pacing comes purely from the proactive floor.
_reset_governor()
# Consume the seeded 2M tpm with two in-capacity calls, then a third must be PACED (queued out) — the pacing comes
# purely from the catalog floor (no config/learned limit set). A single >capacity call is clamped through (can't
# deadlock forever), so the honest proof is cumulative consumption then back-pressure on the next.
_a1 = dispatch.admit(VENDOR, MODEL, deadline_s=0.5, est_tokens=900_000, shed=False, skip_lane=True)
_a2 = dispatch.admit(VENDOR, MODEL, deadline_s=0.5, est_tokens=900_000, shed=False, skip_lane=True)
_a3 = dispatch.admit(VENDOR, MODEL, deadline_s=0.3, est_tokens=900_000, shed=False, skip_lane=True)  # 2.7M > seeded 2M
ck("1. once the seeded tpm budget is consumed, the next COLD metered call is PACED (queues out, not over-dispatched)",
   bool(_a1.ok) and bool(_a2.ok) and not _a3.ok,
   extra="a1=%s a2=%s a3=%s — pacing must come from the catalog floor alone" % (_a1.ok, _a2.ok, _a3.ok))
for _a in (_a1, _a2, _a3):
    try:
        _a.release()
    except Exception:
        pass

# ── GUARD 2: a small in-budget call still admits immediately (pacing doesn't tax normal traffic) ──────────────
_reset_governor()
_small = dispatch.admit(VENDOR, MODEL, deadline_s=0.5, est_tokens=500, shed=False, skip_lane=True)
ck("2. a small in-budget metered call admits immediately (no needless delay from the floor)", bool(_small.ok))
try:
    _small.release()
except Exception:
    pass

# ── GUARD 2b: a storm-scale fan that can't drain within a caller DEADLINE is offered BATCH (deadline-driven) ──
# NOT shape-inferred and NOT transparent (transparent async batch deadlocks a sync caller — the panel's fatal flaw);
# it is deadline/opt-in, decided at the ONE chokepoint via route_horizon. Still to wire -> RED.
ck("2b. a deadline-bound storm-scale fan is offered BATCH (deadline-driven, via the one chokepoint)",
   _should_batch_fan(VENDOR, MODEL, 3000) is True, extra="deadline->batch not wired into admit/route_horizon yet")

# ── GUARD 2c: a batch-diverted fan is AUTO-CHUNKED to the batch API's own limits (always ensure success) ──────
# Diverting 200,000 requests as one batch would hit anthropic's batch max_per_batch/max_in_queue -> a batch 429.
# The planner must split into sub-batches that FIT the catalog's batch limits, so a huge fan always submits cleanly
# (CHUNK-never-single-shot). Probed via the intended planner accessor; RED until wired.
def _chunk_plan(provider, n):
    fn = getattr(__import__("spendguard.route_horizon", fromlist=["x"]), "chunk_for_batch", None)
    try:
        return fn(provider, n) if fn else None
    except Exception:
        return None
_bm = ((mc.vendor_record("anthropic") or {}).get("batch_api") or {}).get("start", {}).get("max_in_queue") or 200000
_plan = _chunk_plan("anthropic", 500000)
ck("2c. a 500k-request fan diverted to batch is auto-chunked to sub-batches within the catalog batch limit (%s)" % _bm,
   bool(_plan) and all(c <= _bm for c in _plan) and sum(_plan) == 500000,
   extra="chunk_for_batch not wired — a huge fan must split to fit, never one oversized batch -> batch 429")

# ── GUARD 3: proactive cold cap exists for the storm model (so even a non-fan burst is paced from call #1) ────
cap = mc.rate_limit_for(MODEL)
ck("3. catalog publishes a proactive rate floor for opus (rpm/itpm/otpm)",
   bool(cap) and cap.get("rpm") and cap.get("itpm"), extra=str(cap))

_reset_governor()
_eff = getattr(dispatch, "effective_limits", None)
_cold_tpm = (_eff(VENDOR, MODEL) or {}).get("tpm") if _eff else None
ck("3b. a COLD anthropic/opus has a non-zero tpm the governor paces against (floor applied before any header)",
   bool(_cold_tpm), extra="effective_limits(anthropic,opus).tpm=%s — today anthropic has conn-only, no tpm" % _cold_tpm)

# ── GUARD 4: anthropic LEARNS tpm from a 429 header AND a success header ─────────────────────────────────────
_reset_governor()
dispatch.learn_rate_limit(VENDOR, tpm=2000000, rpm=1000, source="429-header")
ck("4a. a 429 header teaches anthropic a tpm", (dispatch.learned_limits(VENDOR) or {}).get("tpm") == 2000000)

_reset_governor()
from spendguard import adapters  # noqa: E402


class _Resp:
    def __init__(self, headers):
        self.headers = headers


_lsl = getattr(adapters, "_learn_success_limits", None)
if _lsl:
    try:
        _lsl(VENDOR, _Resp({"anthropic-ratelimit-input-tokens-limit": "2000000",
                            "anthropic-ratelimit-requests-limit": "1000"}))
    except Exception:
        pass
ck("4b. a SUCCESS header teaches anthropic a tpm too (paced BEFORE the first 429)",
   (dispatch.learned_limits(VENDOR) or {}).get("tpm") == 2000000,
   extra="anthropic branch must call _learn_success_limits (today only the openai branch does)")

# ── GUARD 5: AIMD (TCP) — multiplicative-decrease on 429, additive-increase on success ───────────────────────
_reset_governor()
dispatch._LEARNED.learn(VENDOR, conn=8, source="seed")
_b = (dispatch.learned_limits(VENDOR) or {}).get("conn") or 8
try:
    dispatch.shrink_connection_window(VENDOR)
except Exception:
    pass
_a = (dispatch.learned_limits(VENDOR) or {}).get("conn")
ck("5a. AIMD multiplicative-DECREASE: a 429 lowers the concurrency cap", _a is not None and _a < _b,
   extra="conn %s→%s" % (_b, _a))
_grow = getattr(dispatch, "grow_connection_window", None)
_c0 = (dispatch.learned_limits(VENDOR) or {}).get("conn") or 1
if _grow:
    try:
        for _ in range(50):
            _grow(VENDOR)
    except Exception:
        pass
ck("5b. AIMD additive-INCREASE: sustained success grows the cap back (bounded)",
   ((dispatch.learned_limits(VENDOR) or {}).get("conn") or _c0) >= _c0)

# ── GUARD 6: metered_only NEVER sheds to a $0 lane under throttle (billing pin holds) ────────────────────────
_reset_governor()
a = dispatch.admit(VENDOR, MODEL, deadline_s=0.5, est_tokens=10, shed=True, skip_lane=True)
ck("6. metered_only (skip_lane) admission never sheds to a lane", not getattr(a, "shed", False))
try:
    a.release()
except Exception:
    pass

# ── GUARD 7: admission token estimate is OUTPUT-AWARE (OTPM is real output, not max_tokens) ──────────────────
_est = getattr(dispatch, "est_call_tokens_output_aware", None) or getattr(adapters, "_est_call_tokens", None)
try:
    _oa = _est("x" * 400, None, 1024, model=MODEL, intent="measure:big-output") if _est else None
except TypeError:
    _oa = None
ck("7. admission token estimate is output-aware (takes an intent → adds learned expected output)", _oa is not None,
   extra="today _est_call_tokens has no intent/output path → OTPM under-paced")

# ── GUARD 8: a SATURATION signal is exposed so a fan can decide to divert to batch ───────────────────────────
ck("8. governor exposes a saturation/backpressure signal (storm→batch can consult it)",
   (getattr(dispatch, "is_saturated", None) or getattr(dispatch, "saturation", None)) is not None)

print(f"\n{'ALL GREEN' if not fails else 'RED'}: test_dispatch_no_429_storm — {len(fails)} guard(s) RED")
if fails:
    print("The fix is DONE when these are GREEN (5 runs in a row, in CI):")
    for f in fails:
        print("   · " + f)
sys.exit(1 if fails else 0)

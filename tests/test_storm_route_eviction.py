"""4b under DEFAULT-ON must not grow one persistent planner thread per call-shape forever. storm_route reaps
coalescers whose shape has gone quiet. This pins the three properties that make the reaper safe and bounded:

  TTL      — a coalescer idle (no queue, no in-flight) longer than the idle TTL is closed and removed.
  BUSY     — a coalescer with an in-flight cohort is NEVER closed, however old, so eviction cannot abandon work.
  CAP      — over the registry cap, the least-recently-used IDLE shapes are closed down toward the cap.

Offline ($0): no dispatch, no network — `_rate_for` is stubbed so a coalescer can be created for a bare shape, and
the clock is injected so age is deterministic.
"""
import os
import sys
import tempfile
from concurrent.futures import Future

os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="sg-4b-evict-")
os.environ.setdefault("SPENDGUARD_TEST_ISOLATED", "1")
os.environ.setdefault("SPENDGUARD_NO_AUTOINSTALL", "1")
_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(_HERE), "src"))

import spendguard  # noqa: E402
spendguard.require = lambda: None
from spendguard import storm_route, dispatch  # noqa: E402

dispatch.rate_per_s = lambda v, m=None: 10.0       # fixed rate → no real dispatch; a coalescer can be built for a bare shape
TTL = storm_route._idle_ttl_s()
fails = []


def verify_condition(name, cond, extra=""):
    print(("  [OK]   " if cond else "  [RED]  ") + name + (("  — " + extra) if extra and not cond else ""))
    if not cond:
        fails.append(name)


MODEL = "anthropic:claude-haiku-4-5"               # a valid provider:model (default_batch_executor resolves its provider)


def _mk(intent, t):
    """Create (or fetch) the coalescer for one shape (shapes differ by intent), stamping last-used at injected clock t."""
    return storm_route.get_coalescer("anthropic", MODEL, intent, clock=lambda _t=t: _t)


# ── TTL: an idle, aged coalescer is reaped; a fresh one is kept ───────────────────────────────────────────────────
storm_route.reset_registry()
c = _mk("ttl", 1000.0)
verify_condition("created one coalescer, and it reports idle", len(storm_route._REG) == 1 and c.is_idle(),
                 extra="registry=%d idle=%s" % (len(storm_route._REG), c.is_idle()))
verify_condition("within TTL → kept", storm_route._evict_idle(1000.0) == 0 and len(storm_route._REG) == 1,
                 extra="registry=%d" % len(storm_route._REG))
verify_condition("past TTL + idle → reaped", storm_route._evict_idle(1000.0 + TTL + 1) == 1 and len(storm_route._REG) == 0,
                 extra="registry=%d" % len(storm_route._REG))

# ── BUSY: an in-flight cohort protects the coalescer from eviction at ANY age (never abandon work) ────────────────
storm_route.reset_registry()
c = _mk("busy", 2000.0)
fut = Future()                                     # a not-done future == an in-flight routing/batch cohort
with c._lock:
    c._inflight.append(fut)
verify_condition("coalescer with an in-flight future is NOT idle", not c.is_idle())
verify_condition("busy coalescer is kept even far past TTL (no abandon)",
                 storm_route._evict_idle(2000.0 + TTL + 999) == 0 and len(storm_route._REG) == 1,
                 extra="registry=%d" % len(storm_route._REG))
fut.set_result({"text": "done"})                   # cohort completes → now idle
verify_condition("once the cohort completes, the aged coalescer is reaped",
                 storm_route._evict_idle(2000.0 + TTL + 999) == 1 and len(storm_route._REG) == 0,
                 extra="registry=%d" % len(storm_route._REG))

# ── CAP: over the cap, least-recently-used IDLE shapes are reaped; the newest survive ─────────────────────────────
storm_route.reset_registry()
os.environ["SPENDGUARD_STORM_REG_MAX"] = "2"
for i, t in enumerate([10.0, 20.0, 30.0, 40.0]):   # 4 distinct shapes, increasing last-used; all within TTL of each other
    _mk("cap%d" % i, t)
reaped = storm_route._evict_idle(45.0)             # within TTL → only the CAP pass fires; cap=2 → keep the 2 newest
survivors = set(k[2] for k in storm_route._REG)    # key = (vendor, model, intent, reasoning, sysh); intent at index 2
verify_condition("cap enforced: registry reduced to the cap (2)", len(storm_route._REG) == 2,
                 extra="registry=%d reaped(this call)=%d" % (len(storm_route._REG), reaped))
verify_condition("cap evicts the LEAST-recently-used idle shapes (newest two survive)",
                 survivors == {"cap2", "cap3"}, extra="survivors=%s" % survivors)
os.environ.pop("SPENDGUARD_STORM_REG_MAX", None)

# ── get_coalescer OPPORTUNISTICALLY reaps on the next get (the hot-path wiring, via the injected clock) ───────────
storm_route.reset_registry()
_mk("opp-a", 5000.0)
verify_condition("shape A present before the aged next-get", len(storm_route._REG) == 1)
_mk("opp-b", 5000.0 + TTL + 5)                     # a get for a NEW shape, far later → its internal _evict_idle reaps A
survivors2 = set(k[2] for k in storm_route._REG)
verify_condition("the next get_coalescer reaped the quiet shape A and kept the new shape B",
                 survivors2 == {"opp-b"}, extra="survivors=%s" % survivors2)

# ── OVER-CAP but all BUSY: the cap cannot be enforced (never abandon active work) — but it must be SURFACED, not
# silently exceeded (the coding:python doctrine finding). ────────────────────────────────────────────────────────
import io  # noqa: E402
import contextlib  # noqa: E402

storm_route.reset_registry()
os.environ["SPENDGUARD_STORM_REG_MAX"] = "2"
busy_futs = []
for i, t in enumerate([1.0, 2.0, 3.0]):            # 3 shapes, cap 2 → one over cap
    c = _mk("busy%d" % i, t)
    f = Future()                                   # not-done ⇒ an in-flight cohort ⇒ is_idle() False ⇒ NOT reapable
    with c._lock:
        c._inflight.append(f)
    busy_futs.append(f)
_cap_err = io.StringIO()
with contextlib.redirect_stderr(_cap_err):
    reaped_busy = storm_route._evict_idle(100.0)   # within TTL; cap pass runs but every excess is BUSY
_warned = "over cap" in _cap_err.getvalue() and "SPENDGUARD_STORM_REG_MAX" in _cap_err.getvalue()
verify_condition("over-cap + all busy: nothing reaped (active cohorts never abandoned)",
                 reaped_busy == 0 and len(storm_route._REG) == 3,
                 extra="reaped=%d registry=%d" % (reaped_busy, len(storm_route._REG)))
verify_condition("over-cap that could NOT be enforced is SURFACED on stderr (not silent)", _warned,
                 extra="stderr=%r" % _cap_err.getvalue()[:160])
for f in busy_futs:
    f.set_result({"text": "x"})                    # release so reset_registry can close cleanly
os.environ.pop("SPENDGUARD_STORM_REG_MAX", None)

storm_route.reset_registry()
print("\n%s: test_storm_route_eviction — %d checks RED" % ("ALL GREEN" if not fails else "RED", len(fails)))
sys.exit(1 if fails else 0)

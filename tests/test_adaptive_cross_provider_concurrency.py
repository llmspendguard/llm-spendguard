"""Cross-provider ADAPTIVE concurrency — the HIGH-limit (grow) side of the two-sided AIMD connection window.

test_connection_storm_reliability proves the LOW-limit side: a touchy vendor (real ceiling 2-6) is discovered by
backing OFF. This proves the other half the user's goal needs ("maximum throughput — as many concurrent as 429 allows,
per provider"): a TOLERANT vendor RAMPS UP past the old flat cap of 8 toward its ceiling, and the cold start is bounded
to a floor so a generous ceiling never cold-starts a 429 storm.

Four proofs, all offline (no live model calls):
  1. floor/ceiling RESOLUTION — default (8/32), per-vendor override, and the floor clamped below an explicit low ceiling.
  2. _conn_admit COLD gate == floor (NOT unbounded) — a cold vendor admits exactly _vendor_floor, the next blocks.
  3. grow CLIMBS from the floor to the ceiling (seed-then-+1 per window) and STOPS at the ceiling.
  4. END-TO-END — a tolerant mock vendor under a sustained burst reaches LIVE concurrency above the old cap of 8,
     bounded by its ceiling, with ZERO 429s (a clean ramp, not a storm).
"""
import os
import sys
import tempfile
import threading
import time
import functools
import pathlib

# Set every config override UNCONDITIONALLY and BEFORE interpreter startup imports config (the gate's sitecustomize
# imports spendguard at startup), then re-exec once into an isolated home — the standalone-passes / suite-fails trap
# the metered cross-process test documents.
os.environ["SPENDGUARD_DISPATCH_XP_OFF"] = "1"             # in-process buckets only (hermetic; no flock across procs)
os.environ["SPENDGUARD_ROUTE_THROUGH_QUEUE"] = "0"
os.environ.setdefault("SPENDGUARD_NO_AUTOINSTALL", "1")
os.environ["SPENDGUARD_DISPATCH_VENDOR_CONCURRENCY_FLOOR_COLDV"] = "3"     # proof 2: small, explicit cold floor
os.environ["SPENDGUARD_DISPATCH_VENDOR_CONCURRENCY_GROWV"] = "6"           # proof 3: ceiling the window climbs to
os.environ["SPENDGUARD_DISPATCH_VENDOR_CONCURRENCY_FLOOR_GROWV"] = "2"     # proof 3: floor the window seeds at
os.environ["SPENDGUARD_DISPATCH_VENDOR_CONCURRENCY_ANTHROPIC"] = "16"      # proof 4: tolerant-vendor ceiling (> old 8)
os.environ["SPENDGUARD_DISPATCH_VENDOR_CONCURRENCY_HARDCAP"] = "1"         # proof 1: explicit low ceiling clamps the floor
if not os.environ.get("SPENDGUARD_TEST_ISOLATED"):
    os.environ["SPENDGUARD_TEST_ISOLATED"] = "1"
    os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="sg-adaptive-cc-")
    os.execv(sys.executable, [sys.executable] + sys.argv)

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))
from spendguard import adapters, dispatch, lane_balance   # noqa: E402

print = functools.partial(print, flush=True)
fails = 0


def ck(label, cond, extra=""):
    global fails
    if not cond:
        fails += 1
    print(f"  [{'OK' if cond else 'FAIL'}] {label}" + (f"  ({extra})" if extra else ""))


# ── Proof 1: floor/ceiling resolution ────────────────────────────────────────────────────────────────────────
print("-- floor/ceiling resolution --")
ck("default ceiling is DEFAULT_VENDOR_CONCURRENCY_CEILING",
   dispatch._vendor_ceiling("freshvendor") == dispatch.DEFAULT_VENDOR_CONCURRENCY_CEILING,
   f"got {dispatch._vendor_ceiling('freshvendor')}")
ck("default floor is DEFAULT_VENDOR_CONCURRENCY_FLOOR",
   dispatch._vendor_floor("freshvendor") == dispatch.DEFAULT_VENDOR_CONCURRENCY_FLOOR,
   f"got {dispatch._vendor_floor('freshvendor')}")
ck("per-vendor ceiling override is applied (growv=6)", dispatch._vendor_ceiling("growv") == 6)
ck("per-vendor floor override is applied (coldv floor=3)", dispatch._vendor_floor("coldv") == 3)
# An explicit low ceiling must WIN over the default floor — the floor is clamped to it, never raised past it.
ck("floor is clamped below an explicit low ceiling (hardcap=1 → floor 1, not 8)",
   dispatch._vendor_floor("hardcap") == 1 and dispatch._vendor_ceiling("hardcap") == 1,
   f"floor={dispatch._vendor_floor('hardcap')} ceil={dispatch._vendor_ceiling('hardcap')}")

# ── Proof 2: _conn_admit cold gate == floor (not unbounded) ──────────────────────────────────────────────────
print("-- _conn_admit cold gate == floor --")
dispatch.reset_connection_window("coldv")
floor = dispatch._vendor_floor("coldv")          # == 3
admitted = 0
for _ in range(floor):
    dispatch._GOV._conn_admit("vendor:coldv", "coldv", 2.0)
    admitted += 1
ck("a cold vendor admits exactly its floor with no learned cap", admitted == floor, f"admitted {admitted}/{floor}")
blocked = False
try:
    dispatch._GOV._conn_admit("vendor:coldv", "coldv", 0.3)   # the (floor+1)th must block — cold cap is the floor, not ∞
except dispatch.DispatchTimeout:
    blocked = True
ck("the (floor+1)th cold admit BLOCKS — cold admission is bounded by the floor, never unbounded", blocked)
for _ in range(admitted):
    dispatch._GOV._conn_release("vendor:coldv")

# ── Proof 3: grow climbs from floor to ceiling, then stops ───────────────────────────────────────────────────
print("-- grow climbs floor → ceiling → stop --")
dispatch.reset_connection_window("growv")
ceil = dispatch._vendor_ceiling("growv")         # == 6
flr = dispatch._vendor_floor("growv")            # == 2
observed = []
for _ in range(ceil + 2):                        # enough calls to reach the ceiling and prove it stops there
    dispatch._GROW_AT.pop("growv", None)         # bypass the _GROW_WIN debounce for a deterministic climb
    dispatch.grow_connection_window("growv")
    observed.append(dispatch.learned_limits("growv").get("conn"))
ck("grow seeds at the floor then additively climbs to the ceiling and stops",
   observed[0] == flr and max(observed) == ceil and observed[-1] == ceil,
   f"observed={observed}")

# ── Proof 4: end-to-end — a tolerant vendor exceeds the old cap of 8, bounded by its ceiling, with 0 429s ──────
print("-- end-to-end: tolerant vendor ramps above the old cap of 8 --")
MODEL, VENDOR = "claude-haiku-4-5", "anthropic"
TOLERANT_CEILING = dispatch._vendor_ceiling(VENDOR)   # == 16 (env override above)


class _ConnCountingProvider:
    """Stub transport: 429s ONLY above `ceiling` (set comfortably ABOVE the vendor's configured cap, so a healthy
    ramp never trips it), and records the peak live concurrency so the test PROVES the window ramps."""
    def __init__(self, ceiling, latency):
        self.ceiling, self.latency = ceiling, latency
        self.lock = threading.Lock()
        self.inflight = self.max_inflight = self.conn_429s = 0

    def __call__(self, model, prompt, max_tokens=None, **kw):
        with self.lock:
            self.inflight += 1
            self.max_inflight = max(self.max_inflight, self.inflight)
            over = self.inflight > self.ceiling
        try:
            if over:
                with self.lock:
                    self.conn_429s += 1
                time.sleep(0.002)
                return {"provider": VENDOR, "model": model, "text": None, "parsed": None, "in_tok": 0, "out_tok": 0,
                        "cost": 0.0, "latency": 0.002, "finish_reason": None, "truncated": False,
                        "status_code": 429, "outcome": "overloaded",
                        "error": "rate_limit_error: Number of concurrent connections has exceeded your rate limit"}
            time.sleep(self.latency)
            return {"provider": VENDOR, "model": model, "text": "ok", "parsed": None, "in_tok": 12, "out_tok": 8,
                    "cost": 0.00001, "latency": self.latency, "finish_reason": "stop", "truncated": False, "error": None}
        finally:
            with self.lock:
                self.inflight -= 1


dispatch.reset_connection_window(VENDOR)
# Mock ceiling 20 is ABOVE the configured cap 16, so a clean ramp to 16 never 429s. Latency 0.1s keeps the burst
# saturated long enough (300 tasks) for the additive climb (one step / _GROW_WIN=0.2s) to rise well past 8.
prov = _ConnCountingProvider(ceiling=20, latency=0.1)
_real_once, _real_lane_for = adapters._call_once, adapters._lane_for
adapters._call_once = prov
adapters._lane_for = lambda v: None                  # force the METERED (vendor:) path
adapters._resolve_guard.on = True                    # no served-substitute resolver (no network / model swap)
burst = 300
rows, err = [], None
t0 = time.time()
try:
    rows = lane_balance.bulk_delegate(["hello"] * burst, "adaptive:probe",
                                      model_for=lambda t: f"{VENDOR}:{MODEL}", metered_only=True,
                                      max_workers=burst, deadline_s=60.0, force=True)
except Exception as e:
    err = f"{type(e).__name__}: {e}"
finally:
    adapters._call_once, adapters._lane_for = _real_once, _real_lane_for
    adapters._resolve_guard.on = False
client_fail = sum(1 for r in rows if not (r or {}).get("text")) if rows else burst
learned = dispatch.learned_limits(VENDOR).get("conn")
print(f"  burst={burst} max_inflight={prov.max_inflight} conn_429s={prov.conn_429s} "
      f"client_fail={client_fail} learned_conn={learned} {time.time()-t0:.1f}s ceiling={TOLERANT_CEILING}")
ck("every result returned (no client failure)", client_fail == 0 and err is None, f"client_fail={client_fail} err={err}")
ck("a tolerant vendor ramps live concurrency ABOVE the old flat cap of 8",
   prov.max_inflight > 8, f"max_inflight={prov.max_inflight}")
ck("live concurrency stays BOUNDED by the vendor's ceiling",
   prov.max_inflight <= TOLERANT_CEILING, f"max_inflight={prov.max_inflight} ceiling={TOLERANT_CEILING}")
ck("a healthy ramp trips ZERO 429s (it grew INTO the limit, it did not storm it)",
   prov.conn_429s == 0, f"conn_429s={prov.conn_429s}")

print(f"\n{'OK' if fails == 0 else '[FAIL]'} test_adaptive_cross_provider_concurrency: {fails} failure(s)")
sys.exit(1 if fails else 0)

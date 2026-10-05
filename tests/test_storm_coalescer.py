"""Unit proof of StormCoalescer in ISOLATION (offline, $0, deterministic) — one check per invariant in the
INVARIANT -> TEST MAP (docs/PLAN_429_storm_to_batch.md). Drives the coalescer against tests/storm_harness.FakeProvider
via injected execute_realtime/execute_batch. This proves the ROUTING+TIMING logic (the combo, demux, conservation,
pacing, logical break, partial-batch refill, no-pending-on-error, chunk coverage). The end-to-end wiring through
bulk_delegate is proved separately by tests/test_incident_replay_storm.py.

Every check is un-fakeable the same way the harness is: the FakeProvider 429s a real burst (so "paced => 0 429" has
teeth), echoes each request's canary (so demux is checkable), and counts realtime-vs-batch at the provider.
"""
import concurrent.futures as cf
import os
import random
import sys
import tempfile
import threading
import time

os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="sg-coalescer-")
os.environ.setdefault("SPENDGUARD_TEST_ISOLATED", "1")
os.environ.setdefault("SPENDGUARD_NO_AUTOINSTALL", "1")
_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(_HERE), "src"))
sys.path.insert(0, _HERE)

import spendguard  # noqa: E402
spendguard.require = lambda: None
import storm_harness as H  # noqa: E402
from spendguard.storm_coalescer import StormCoalescer  # noqa: E402

fails = []
total = [0]


def ck(name, cond, extra=""):
    total[0] += 1
    print(("  [OK]   " if cond else "  [RED]  ") + name + (("  — " + extra) if extra and not cond else ""))
    if not cond:
        fails.append(name)


def _rt(fp, model="m"):
    return lambda req: fp._realtime(model, req)


def _batch(fp, model="m"):
    """ID-keyed batch adapter: items=[(custom_id, req)] -> {custom_id: result}. The FakeProvider is already id-keyed."""
    def _b(items):
        return fp.batch_submit([(cid, model, req) for cid, req in items])
    return _b


def _results(futs):
    return [f.result(timeout=30) for f in futs]


# ── I1 — conservation: N submitted -> N returned, each future resolved exactly once ───────────────────────────
print("-- I1 conservation (N in == N out; every future resolved once) --")
for N in (0, 1, 2, 300, 1000):
    fp = H.FakeProvider(cap=100000, window_s=1.0)                 # huge wall: isolate conservation from pacing
    c = StormCoalescer(execute_realtime=_rt(fp), execute_batch=_batch(fp),
                       sustainable_rate_per_s=5000, horizon_s=1.0, provider="anthropic")
    futs = c.submit_all(["do %s" % H.canary(i) for i in range(N)])
    res = _results(futs)
    c.close()
    all_done = all(f.done() for f in futs)
    ok_n = sum(1 for r in res if isinstance(r, dict) and r.get("text") and not r.get("error"))
    ck("I1 N=%d -> exactly N resolved results" % N, len(res) == N and ok_n == N and all_done,
       extra="len=%d ok=%d done=%s" % (len(res), ok_n, all_done))
# concurrent arm (panel: serial submit_all under a huge cap didn't exercise contention): 200 threads, a REAL wall
fp = H.FakeProvider(cap=60, window_s=1.0)
c = StormCoalescer(execute_realtime=_rt(fp), execute_batch=_batch(fp),
                   sustainable_rate_per_s=40, horizon_s=1.0, provider="anthropic")   # budget 40, N 200 => both paths, paced
subs = ["do %s" % H.canary(i) for i in range(200)]
box = [None] * 200
with cf.ThreadPoolExecutor(max_workers=32) as ex:
    list(ex.map(lambda i: box.__setitem__(i, c.submit_request(subs[i])), range(200)))
res = [f.result(timeout=30) for f in box]
c.close()
ok_n = sum(1 for r in res if r.get("text") and not r.get("error"))
no_surfaced_429 = all((r.get("status_code") not in (429, 529)) and "429" not in str(r.get("error") or "") for r in res)
demux_ok = all(H._extract_canary(res[i].get("text")) == H._extract_canary(subs[i]) for i in range(200))
ck("I1 concurrent: 200 submitted from 32 threads under a real wall -> all resolved, demux ok, 0 SURFACED 429",
   ok_n == 200 and no_surfaced_429 and demux_ok,
   extra="ok=%d/200 surfaced_clean=%s demux=%s rt429_absorbed=%d" % (ok_n, no_surfaced_429, demux_ok, fp.realtime_429))

# ── I2 — demux/bijection: future for request i resolves with the result FOR i ─────────────────────────────────
print("-- I2 demux/bijection (future_i answers request_i, across paths) --")
fp = H.FakeProvider(cap=100000, window_s=1.0)
c = StormCoalescer(execute_realtime=_rt(fp), execute_batch=_batch(fp),
                   sustainable_rate_per_s=50, horizon_s=1.0, provider="anthropic")   # budget 50, N 200 => both paths
subs = ["do %s" % H.canary(i) for i in range(200)]
futs = c.submit_all(subs)
res = _results(futs)
c.close()
mismatch = [i for i, (r, s) in enumerate(zip(res, subs))
            if H._extract_canary(r.get("text")) != H._extract_canary(s)]
ck("I2 every future_i carries canary_i (demux correct across realtime+batch)", not mismatch,
   extra="%d mismatches e.g. idx %s" % (len(mismatch), mismatch[:5]))

# ── I5 — realtime egress PACED to the sustainable rate: SLIDING-WINDOW, not just elapsed (panel: elapsed is loose) ─
print("-- I5 realtime paced <= per-second cap over any sliding 1s window (teeth: unpaced would 429) --")
fp = H.FakeProvider(cap=15, window_s=1.0)                         # wall 15/s: an unpaced 20-burst would 429
_stamps = []
_slock = threading.Lock()
def _rt_stamped(req):
    r = fp._realtime("m", req)
    with _slock:
        _stamps.append(time.monotonic())
    return r
c = StormCoalescer(execute_realtime=_rt_stamped, execute_batch=_batch(fp),
                   sustainable_rate_per_s=10, horizon_s=2.0, provider="anthropic")    # budget 20, N 20 => realtime-only
t0 = time.monotonic()
futs = c.submit_all(["do %s" % H.canary(i) for i in range(20)])
res = _results(futs)
elapsed = time.monotonic() - t0
c.close()
_ss = sorted(_stamps)
peak_1s = max((sum(1 for b in _ss if a <= b < a + 1.0) for a in _ss), default=0)   # max releases in ANY sliding 1s
ck("I5 sliding-window peak <= per-second cap(+1), 0 provider 429, not instant",
   peak_1s <= 11 and fp.realtime_429 == 0 and fp.realtime_calls == 20 and elapsed > 1.4,
   extra="peak_1s=%d (cap 10) 429=%d served=%d elapsed=%.2fs" % (peak_1s, fp.realtime_429, fp.realtime_calls, elapsed))

# ── I6 — the COMBO under pressure: pace sustainable share AND batch the overflow ──────────────────────────────
print("-- I6 combo (realtime 0<x<=budget AND overflow batched) --")
fp = H.FakeProvider(cap=100000, window_s=1.0)
c = StormCoalescer(execute_realtime=_rt(fp), execute_batch=_batch(fp),
                   sustainable_rate_per_s=40, horizon_s=1.0, provider="anthropic")    # budget 40
N = 300
futs = c.submit_all(["do %s" % H.canary(i) for i in range(N)])
res = _results(futs)
c.close()
ok_n = sum(1 for r in res if isinstance(r, dict) and r.get("text") and not r.get("error"))
ck("I6 combo: realtime in (0,budget], overflow batched, all N returned",
   0 < c.realtime_served <= c.realtime_budget + 2 and c.batch_jobs > 0
   and c.batch_served >= N - c.realtime_budget - 2 and ok_n == N,
   extra="rt_served=%d (budget %d) batch_jobs=%d batch_served=%d ok=%d/%d"
         % (c.realtime_served, c.realtime_budget, c.batch_jobs, c.batch_served, ok_n, N))

# ── I7 — below threshold: small cohort stays realtime, ZERO batch (no over-eager batching) ────────────────────
print("-- I7 small cohort stays realtime (no over-eager batch) --")
fp = H.FakeProvider(cap=100000, window_s=1.0)
c = StormCoalescer(execute_realtime=_rt(fp), execute_batch=_batch(fp),
                   sustainable_rate_per_s=40, horizon_s=1.0, provider="anthropic")    # budget 40
futs = c.submit_all(["do %s" % H.canary(i) for i in range(5)])   # 5 << budget
res = _results(futs)
c.close()
ck("I7 small cohort: 0 batch jobs, all 5 served realtime", c.batch_jobs == 0 and c.realtime_served == 5,
   extra="batch_jobs=%d rt_served=%d" % (c.batch_jobs, c.realtime_served))

# ── I8 — a burst within the idle gap forms ONE cohort (planned once) ──────────────────────────────────────────
print("-- I8 coalesces a burst into ONE cohort --")
fp = H.FakeProvider(cap=100000, window_s=1.0)
c = StormCoalescer(execute_realtime=_rt(fp), execute_batch=_batch(fp),
                   sustainable_rate_per_s=100000, horizon_s=10.0, provider="anthropic",  # budget huge => wait for quiescence
                   idle_gap_s=0.05)
futs = [c.submit_request("do %s" % H.canary(i)) for i in range(50)]      # tight loop << idle_gap
_results(futs)
c.close()
ck("I8 one burst -> one cohort (O(1) plan, not per-request)", c.cohorts == 1, extra="cohorts=%d" % c.cohorts)

# ── I9 — logical break: a lone call fires at ~idle_gap, NOT at max_wait ───────────────────────────────────────
print("-- I9 logical-break timing (lone call not needlessly delayed) --")
fp = H.FakeProvider(cap=100000, window_s=1.0)
c = StormCoalescer(execute_realtime=_rt(fp), execute_batch=_batch(fp),
                   sustainable_rate_per_s=100000, horizon_s=1.0, provider="anthropic",
                   idle_gap_s=0.02, max_wait_s=1.0)
t0 = time.monotonic()
r = c.submit_request("do %s" % H.canary(0)).result(timeout=5)
lone = time.monotonic() - t0
c.close()
ck("I9 lone call resolves at ~idle_gap (idle_gap <= lone << max_wait; panel: add the LOWER bound)",
   bool(r.get("text")) and 0.01 <= lone < 0.3,
   extra="latency=%.3fs (want 0.01<=lone<0.3; idle_gap=0.02, max_wait=1.0 — waited the gap, not 0, not the ceiling)" % lone)

# ── I15 — batch demux BY ID survives shuffle + MIDDLE drop + unknown id (the panel's data-corruption finding) ──
print("-- I15 batch demux by id (shuffled / middle-dropped / unknown), never positional --")
fp = H.FakeProvider(cap=100000, window_s=1.0)
_base = _batch(fp)
_drop = {"id": None}
def _evil(items):
    out = dict(_base(items))
    ids = [cid for cid, _ in items]
    if _drop["id"] is None and len(ids) >= 3:
        _drop["id"] = ids[len(ids) // 2]                 # a MIDDLE id — positional zip would corrupt here
    if _drop["id"] in out:
        out.pop(_drop["id"]); _drop["id"] = "DONE"       # provider drops it ONCE (first attempt); refill then succeeds
    out["ghost-9999"] = {"text": "ok GHOST", "status_code": 200}       # unknown id — must be quarantined, not assigned
    pairs = list(out.items()); random.Random(7).shuffle(pairs)          # returned UNORDERED
    return dict(pairs)
c = StormCoalescer(execute_realtime=_rt(fp), execute_batch=_evil,
                   sustainable_rate_per_s=5, horizon_s=1.0, provider="anthropic")     # budget 5, N 40 => 35 overflow
subs = ["do %s" % H.canary(i) for i in range(40)]
res = _results(c.submit_all(subs))
c.close()
mismatch = [i for i, (r, s) in enumerate(zip(res, subs)) if H._extract_canary(r.get("text")) != H._extract_canary(s)]
ok_n = sum(1 for r in res if r.get("text") and not r.get("error"))
ck("I15 demux-by-id: bijection holds through shuffle+middle-drop+unknown; dropped id refilled; all 40 correct",
   not mismatch and ok_n == 40, extra="mismatches=%s ok=%d/40" % (mismatch[:5], ok_n))

# ── I19 — poison isolation: one item errors EVERY attempt, siblings still succeed (not a whole-group abort) ────
print("-- I19 poison isolation (one bad item does not fail its cohort-mates) --")
fp = H.FakeProvider(cap=100000, window_s=1.0)
_base = _batch(fp)
POISON = H.canary(17)
def _poison(items):
    out = dict(_base(items))
    for cid, req in items:
        if POISON in str(req):
            out[cid] = {"text": None, "error": "invalid request (poison)", "status_code": 400}  # per-item error, always
    return out
c = StormCoalescer(execute_realtime=_rt(fp), execute_batch=_poison,
                   sustainable_rate_per_s=5, horizon_s=1.0, provider="anthropic")     # budget 5, N 30 => 17 is in overflow
res = _results(c.submit_all(["do %s" % H.canary(i) for i in range(30)]))
c.close()
siblings_ok = sum(1 for i, r in enumerate(res) if i != 17 and r.get("text") and not r.get("error"))
ck("I19 poison isolated: item 17 typed-errors, the other 29 succeed, none pending",
   res[17].get("error") and not res[17].get("text") and siblings_ok == 29,
   extra="poison_err=%s siblings_ok=%d/29" % (bool(res[17].get("error")), siblings_ok))

# ── I12 — whole-submit THROW → typed group error, never a pending future (per-item isolation on a throw = I19-bisect) ─
print("-- I12 whole-submit throw -> typed group error, none pending (bisection isolation is a follow-up) --")
fp = H.FakeProvider(cap=100000, window_s=1.0)
def _boom(items):
    raise RuntimeError("batch provider down")
c = StormCoalescer(execute_realtime=_rt(fp), execute_batch=_boom,
                   sustainable_rate_per_s=10, horizon_s=1.0, provider="anthropic")    # budget 10, N 30 => 20 overflow
futs = c.submit_all(["do %s" % H.canary(i) for i in range(30)])
res = _results(futs)
c.close()
all_done = all(f.done() for f in futs)
errs = sum(1 for r in res if isinstance(r, dict) and r.get("error"))
served = sum(1 for r in res if isinstance(r, dict) and r.get("text"))
ck("I12 all futures resolved (served or typed-error), none pending", all_done and errs == 20 and served == 10,
   extra="done=%s errors=%d served=%d (want True,20,10)" % (all_done, errs, served))

# ── I4b (panel fix) — a realtime call that RAISES or returns an ERROR reroutes to BATCH; siblings unaffected ───
print("-- I4b realtime exception/error isolation: the bad item reroutes to batch, one failure can't abort the cohort --")
fp = H.FakeProvider(cap=100000, window_s=1.0)
BOOM, ERRC = H.canary(3), H.canary(7)
def _rt_flaky(req):
    if BOOM in str(req):
        raise RuntimeError("realtime boom")                        # a RAISE must not abort the cohort (old bug)
    if ERRC in str(req):
        return {"text": None, "error": "realtime 500", "status_code": 500}   # an error result must not count as success
    return fp._realtime("m", req)
c = StormCoalescer(execute_realtime=_rt_flaky, execute_batch=_batch(fp),
                   sustainable_rate_per_s=100000, horizon_s=1.0, provider="anthropic")   # huge budget → all start realtime
res = _results(c.submit_all(["do %s" % H.canary(i) for i in range(20)]))
c.close()
ok_n = sum(1 for r in res if r.get("text") and not r.get("error"))
ck("I4b realtime raise + error both reroute to batch, all 20 served, siblings fine",
   ok_n == 20 and res[3].get("served_via") == "batch" and res[7].get("served_via") == "batch",
   extra="ok=%d/20 canary3_via=%s canary7_via=%s" % (ok_n, res[3].get("served_via"), res[7].get("served_via")))

# ── I14 — chunk coverage: slices cover the overflow exactly, no overlap, no dropped tail ──────────────────────
print("-- I14 chunks cover overflow exactly (union==overflow, no overlap) --")
c = StormCoalescer(execute_realtime=lambda r: {}, execute_batch=lambda rs: [{} for _ in rs],
                   sustainable_rate_per_s=10, horizon_s=1.0, provider="openai")        # openai chunks 50000 -> 50x1000
for M in (5, 1000, 50000, 50001):
    pend = list(range(M))
    groups = c._slice_by_chunks(pend)
    flat = [x for g in groups for x in g]
    ck("I14 M=%d: concat(groups)==pend (exact cover, ordered, no overlap/drop)" % M, flat == pend,
       extra="len(flat)=%d vs %d; n_groups=%d" % (len(flat), M, len(groups)))
c.close()

print("\n%s: test_storm_coalescer — %d/%d checks RED" % ("ALL GREEN" if not fails else "RED", len(fails), total[0]))
if fails:
    for f in fails:
        print("   RED:", f)
sys.exit(1 if fails else 0)

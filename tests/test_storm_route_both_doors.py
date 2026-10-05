"""4b COMBO through the RAW door (now deterministic thanks to I20): a concurrent fan of independent labelled
adapters.call at one vendor, with SPENDGUARD_STORM_COALESCE on, gets the pace+batch COMBO — the realtime share is
bounded and the overflow diverts to batch — exactly like the explicit submit_storm entry, but via the implicit door
that actually caused the incident. Offline ($0): realtime → the FakeProvider wall; the anthropic Batch API is mocked
(submit_message_batch/collect_message_batch) with per-batch canary echo so demux is checkable.
"""
import concurrent.futures as cf
import os
import sys
import tempfile
import threading

os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="sg-4b-combo-")
os.environ.setdefault("SPENDGUARD_TEST_ISOLATED", "1")
os.environ.setdefault("SPENDGUARD_NO_AUTOINSTALL", "1")
os.environ["SPENDGUARD_DISPATCH_MANAGE_ALL"] = "1"
os.environ["SPENDGUARD_DISPATCH_RPM_ANTHROPIC"] = "2400"    # 40/s
os.environ["SPENDGUARD_STORM_COALESCE"] = "1"               # 4b ON for this test
os.environ["SPENDGUARD_STORM_HORIZON_S"] = "1"              # budget = 40/s * 1s = 40 → N>>budget forces the combo
_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(_HERE), "src"))
sys.path.insert(0, _HERE)

import spendguard  # noqa: E402
spendguard.require = lambda: None
import storm_harness as H  # noqa: E402
from spendguard import adapters, storm_route, submit as S, callio as C  # noqa: E402

MODEL = "anthropic:claude-haiku-4-5"
N = 100
fails = []


def verify_condition(name, cond, extra=""):
    print(("  [OK]   " if cond else "  [RED]  ") + name + (("  — " + extra) if extra and not cond else ""))
    if not cond:
        fails.append(name)


# mock the anthropic Batch API pair, per-batch, echoing each request's canary so demux is checkable
_bstore, _bn, _blk = {}, {"n": 0}, threading.Lock()
def _fake_submit_msg(tasks, model, **kw):
    with _blk:
        _bn["n"] += 1
        bid = "b%d" % _bn["n"]
        _bstore[bid] = {t["custom_id"]: H._extract_canary(t["content"]) for t in tasks}
    return {"batch_id": bid, "error": None}
def _fake_collect_msg(bid, intent, model, require_ready=True, record_io=False):
    with _blk:
        m = dict(_bstore.get(bid, {}))
    return {"results": {cid: "ok %s" % can for cid, can in m.items()}, "failed": {}, "not_ready": []}
S.submit_message_batch = _fake_submit_msg
C.collect_message_batch = _fake_collect_msg

fp = H.FakeProvider(cap=100000, window_s=1.0).install()
col = H.CallerCollector()
try:
    with cf.ThreadPoolExecutor(max_workers=N) as ex:                 # the raw implicit fan (what honestreview did)
        out = list(ex.map(lambda i: adapters.call(MODEL, "do %s" % H.canary(i), intent="acc:4b-combo",
                                                  sig="acc:4b-combo"), range(N)))
    for r in out:
        col.record(r)
finally:
    storm_route.reset_registry()
    fp.uninstall()

ok_n = sum(1 for r in out if isinstance(r, dict) and r.get("text") and not r.get("error"))
batch_served = sum(len(m) for m in _bstore.values())
realtime_served = fp.realtime_calls
mismatch = [i for i in range(N) if H._extract_canary(out[i].get("text")) != H.canary(i)]

verify_condition("N submitted -> N returned through the raw door (conservation)", ok_n == N, extra="ok=%d/%d" % (ok_n, N))
verify_condition("ZERO surfaced 429s to the caller", col.surfaced_429 == 0, extra="surfaced=%d" % col.surfaced_429)
verify_condition("COMBO: realtime bounded (did NOT all ride realtime) AND batch carried the overflow",
                 0 < realtime_served < N and batch_served > 0,
                 extra="realtime=%d batch=%d (want 0<rt<%d and batch>0)" % (realtime_served, batch_served, N))
verify_condition("conservation at the providers: realtime + batch == N (every item served by exactly one path)",
                 realtime_served + batch_served == N, extra="rt=%d + batch=%d != %d" % (realtime_served, batch_served, N))
verify_condition("demux: result i carries canary i (across both paths)", not mismatch, extra="mismatches=%s" % mismatch[:5])

print("\n%s: test_storm_route_both_doors — %d checks RED  [realtime=%d batch=%d surfaced429=%d]"
      % ("ALL GREEN" if not fails else "RED", len(fails), realtime_served, batch_served, col.surfaced_429))
sys.exit(1 if fails else 0)

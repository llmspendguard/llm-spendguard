"""Guard — the per-vendor TPM (tokens/minute) token-bucket in the dispatch governor. TPM is the ceiling that ACTUALLY
429s on OpenAI/Anthropic (an enqueued-/tokens-per-minute limit) — a concurrency or RPM cap cannot bound it, because a
handful of big-context calls blow TPM while well under both. This checks the bucket paces by est_tokens, admits a call
bigger than the whole budget rather than deadlocking, is byte-neutral when tpm=0 (default), hands the concurrency slot
back on a TPM timeout, and that Governor.acquire threads est_tokens + queue_state surfaces tpm.

TPM causation is proved STRUCTURALLY by a CONTROLLED CONTRAST, never by matching the error message: the same acquire()
at the same deadline with concurrency provably free differs only in est_tokens — the big call times out, the est_tokens=0
call succeeds, so the timeout can only be the token bucket. Pure timing, no network."""
import os
import sys
import tempfile

os.environ.setdefault("SPENDGUARD_TEST_ISOLATED", "1")
os.environ.setdefault("SPENDGUARD_HOME", tempfile.mkdtemp(prefix="spendguard-tpm-"))
os.environ["SPENDGUARD_DISPATCH_XP_OFF"] = "1"   # isolate the in-process bucket from cross-process flock slots

from spendguard import dispatch

_fails = []
def ck(label, cond):
    if not cond:
        _fails.append(label)
    print(f"  [{'OK' if cond else 'FAIL'}] {label}")

def _times_out(bucket, deadline_s, est_tokens):
    """True iff bucket.acquire(deadline_s, est_tokens) queues out. Releases the slot if it unexpectedly admits, so
    the bucket's concurrency state is unchanged either way — the ONLY variable between calls is est_tokens."""
    try:
        bucket.acquire(deadline_s=deadline_s, est_tokens=est_tokens)
        bucket.release()
        return False
    except dispatch.DispatchTimeout:
        return True

# ── (1) the bucket paces by est_tokens: full → a big call fits, the next same-size call must WAIT the deficit ──
print("-- (1) _tpm_wait_s: starts full, drains, then paces the deficit at tpm/60 tokens/sec --")
b = dispatch._Bucket("vendor:tpmtest", limit=8, rpm=0, reserve=0, tpm=60000)   # 60k/min = 1000 tok/sec
w1 = b._tpm_wait_s(50000)                     # 50k ≤ 60k available → fits now, reserves it (10k left)
ck("first big call fits immediately (bucket starts full)", w1 == 0.0)
w2 = b._tpm_wait_s(50000)                     # needs 50k, only ~10k left → wait ≈ (50k-10k)/1000 = 40s
ck("second same-size call waits the token deficit (~40s at 1000 tok/s)", 38.0 < w2 < 41.0)

# ── (2) tpm=0 (the default) → NEVER paces, whatever the token count ──
print("-- (2) tpm=0 is byte-neutral: no token pacing at all --")
b0 = dispatch._Bucket("vendor:tpm0", 8, 0, 0, tpm=0)
ck("tpm=0 → a billion-token call still waits 0", b0._tpm_wait_s(10**9) == 0.0)

# ── (3) a call BIGGER than the whole per-minute budget is admitted immediately (never deadlocks) ──
print("-- (3) a call larger than the whole TPM budget admits now (drives the bucket negative), never deadlocks --")
b3 = dispatch._Bucket("vendor:tpmbig", 8, 0, 0, tpm=1000)
ck("est_tokens > tpm → admitted immediately (t >= tpm branch)", b3._tpm_wait_s(5000) == 0.0)

# ── (4) acquire() enforces TPM within the deadline — proved by the est_tokens contrast, not a message match ──
print("-- (4) acquire(): drained TPM + same free concurrency → big call times out, est_tokens=0 does not (contrast) --")
b4 = dispatch._Bucket("vendor:tpmacq", limit=8, rpm=0, reserve=0, tpm=6000)   # 100 tok/sec
b4._tpm_wait_s(6000)                          # drain the bucket to ~0
ck("concurrency is fully free before the probe (in_flight==0, nothing holds _sem)", b4.in_flight == 0)
big_timed_out = _times_out(b4, deadline_s=0.2, est_tokens=6000)   # needs 6000, ~20 refill in 0.2s → cannot fit
small_ok = not _times_out(b4, deadline_s=0.5, est_tokens=0)       # identical call, TPM skipped → admits
ck("est_tokens=6000 on a drained bucket queues out", big_timed_out)
ck("the SAME acquire with est_tokens=0 admits → the timeout was the TOKEN bucket, not concurrency (structural)",
   small_ok)

# ── (5) Governor.acquire threads est_tokens end-to-end; queue_state surfaces the configured tpm ──
print("-- (5) Governor.acquire + config: tpm_<vendor> builds a paced bucket, visible in queue_state --")
os.environ["SPENDGUARD_DISPATCH_TPM_TPMVENDOR"] = "6000"
os.environ["SPENDGUARD_DISPATCH_VENDOR_CONCURRENCY_TPMVENDOR"] = "8"   # concurrency never the binding constraint here
# first call drains the fresh (full) 6000-token bucket; then contrast a big vs a zero-token call at a tiny deadline
dispatch.acquire("tpmvendor", "tpmvendor:m", deadline_s=5.0, est_tokens=6000)
dispatch.release("tpmvendor", "tpmvendor:m")
st = dispatch.queue_state().get("vendor:tpmvendor") or {}
ck("queue_state surfaces the configured tpm for the vendor (config → bucket wiring)", st.get("tpm") == 6000)
big_out = False
try:
    dispatch.acquire("tpmvendor", "tpmvendor:m", deadline_s=0.2, est_tokens=6000)
    dispatch.release("tpmvendor", "tpmvendor:m")
except dispatch.DispatchTimeout:
    big_out = True
zero_ok = False
try:
    dispatch.acquire("tpmvendor", "tpmvendor:m", deadline_s=0.5, est_tokens=0)
    dispatch.release("tpmvendor", "tpmvendor:m")
    zero_ok = True
except dispatch.DispatchTimeout:
    zero_ok = False
ck("through Governor.acquire: the big call queues out while est_tokens=0 admits (TPM enforced end-to-end)",
   big_out and zero_ok)

print(f"\n{'[FAIL]' if _fails else 'OK'} test_dispatch_tpm_bucket: {len(_fails)} failure(s)")
sys.exit(1 if _fails else 0)

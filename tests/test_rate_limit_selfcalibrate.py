"""Guard — SELF-CALIBRATION (Step 3): the governor learns a vendor's REAL rate limit from its 429 headers, so the
first limit-unknown 429 is the LAST. Without a configured tpm_/rpm_, admission can't pace; this teaches it from the
provider's own x-ratelimit-limit-* headers (+ Retry-After) on the one 429 it takes.

  (1) _ratelimit_from_resp parses OpenAI (x-ratelimit-limit-tokens/-requests) AND Anthropic (anthropic-ratelimit-*-limit)
      per-minute ceilings; no headers → (None, None);
  (2) learn_rate_limit(tpm=) makes dispatch PACE that vendor to the learned tpm even with NO config set — proved by the
      est_tokens contrast (big call queues out, est_tokens=0 admits), and it PERSISTS to disk (a fresh reader sees it);
  (3) a 429's Retry-After cools the vendor — an acquire within a deadline shorter than the cooldown queues out;
  (4) an EXPLICIT dispatch.tpm_<vendor> config still wins over the learned value.
Hermetic: fake response objects + direct dispatch calls; XP flock off; no network."""
import os
import sys
import tempfile

os.environ.setdefault("SPENDGUARD_TEST_ISOLATED", "1")
os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="spendguard-learn-")   # isolate learned_limits.json
os.environ["SPENDGUARD_DISPATCH_XP_OFF"] = "1"                                  # in-process buckets only

from spendguard import adapters, dispatch

_fails = []
def ck(label, cond):
    if not cond:
        _fails.append(label)
    print(f"  [{'OK' if cond else 'FAIL'}] {label}")

def _times_out(vendor, deadline_s, est_tokens):
    try:
        dispatch.acquire(vendor, vendor + ":m", deadline_s=deadline_s, est_tokens=est_tokens)
        dispatch.release(vendor, vendor + ":m")
        return False
    except dispatch.DispatchTimeout:
        return True

class _FakeResp:
    def __init__(self, headers):
        self.headers = headers

# ── (1) header parsing — OpenAI + Anthropic, and empty ──
print("-- (1) _ratelimit_from_resp: parse OpenAI + Anthropic per-minute limit headers --")
tpm, rpm = adapters._ratelimit_from_resp(_FakeResp({"x-ratelimit-limit-tokens": "2000000",
                                                    "x-ratelimit-limit-requests": "10000"}))
ck("OpenAI x-ratelimit-limit-tokens/-requests parsed", tpm == 2000000 and rpm == 10000)
tpm2, rpm2 = adapters._ratelimit_from_resp(_FakeResp({"anthropic-ratelimit-tokens-limit": "400000",
                                                      "anthropic-ratelimit-requests-limit": "4000"}))
ck("Anthropic anthropic-ratelimit-*-limit parsed", tpm2 == 400000 and rpm2 == 4000)
ck("no rate-limit headers → (None, None)", adapters._ratelimit_from_resp(_FakeResp({})) == (None, None))

# ── (2) learn → dispatch paces to the learned tpm (no config), and it persists ──
print("-- (2) learn_rate_limit(tpm) → the vendor is paced to the LEARNED tpm (no config), and persisted to disk --")
V = "learnvendor"
os.environ.pop("SPENDGUARD_DISPATCH_TPM_LEARNVENDOR", None)   # ensure NO explicit config for this vendor
dispatch.learn_rate_limit(V, tpm=6000, source="429-header")  # 100 tok/sec, learned from a (simulated) 429
ll = dispatch.learned_limits(V)
ck("learned_limits records the tpm + source", ll.get("tpm") == 6000 and ll.get("source") == "429-header")
dispatch.acquire(V, V + ":m", deadline_s=5.0, est_tokens=6000)   # drain the fresh (full) learned bucket
dispatch.release(V, V + ":m")
big_out = _times_out(V, deadline_s=0.2, est_tokens=6000)      # needs 6000 more → cannot fit in 0.2s
zero_ok = not _times_out(V, deadline_s=0.5, est_tokens=0)     # identical call, TPM skipped → admits (contrast)
ck("a vendor with ONLY a learned tpm is paced (big call queues out, est_tokens=0 admits)", big_out and zero_ok)
fresh = dispatch._LearnedLimits()                            # a fresh reader (≈ another process / a restart)
ck("the learned limit persisted to disk (a fresh reader sees it)", (fresh.for_vendor(V) or {}).get("tpm") == 6000)

# ── (3) Retry-After cools the vendor ──
print("-- (3) a 429's Retry-After cools the vendor: an acquire within deadline<cooldown queues out --")
V2 = "coolvendor"
dispatch.learn_rate_limit(V2, retry_after_s=30, source="429-header")   # cool 30s
cooled = False
try:
    dispatch.acquire(V2, V2 + ":m", deadline_s=0.2)          # 0.2s deadline << 30s cooldown → DispatchTimeout
    dispatch.release(V2, V2 + ":m")
except dispatch.DispatchTimeout:
    cooled = True
ck("Retry-After cools new admissions (acquire within deadline<cooldown raises)", cooled)

# ── (4) explicit config tpm still wins over the learned value ──
print("-- (4) an explicit dispatch.tpm_<vendor> config overrides the learned tpm --")
V3 = "precvendor"
dispatch.learn_rate_limit(V3, tpm=6000, source="429-header")
os.environ["SPENDGUARD_DISPATCH_TPM_PRECVENDOR"] = "999999"
b = dispatch._GOV._bucket(V3, V3 + ":m")                     # builds/re-keys the bucket with config precedence
ck("explicit config tpm (999999) wins over learned (6000)", b.tpm == 999999)
os.environ.pop("SPENDGUARD_DISPATCH_TPM_PRECVENDOR", None)

# ── (5) SUCCESS-HEADER learning: the limit rides every SUCCESSFUL response → paced BEFORE the first 429 ──
print("-- (5) _learn_success_limits: a successful response's headers teach the limit (no 429 needed) --")
class _FakeResp2:
    def __init__(self, headers):
        self.headers = headers
adapters._learn_success_limits("succvendor", _FakeResp2({"x-ratelimit-limit-tokens": "1200000",
                                                         "x-ratelimit-limit-requests": "5000"}))
ll5 = dispatch.learned_limits("succvendor")
ck("a success response teaches tpm/rpm with source='success-header'",
   ll5.get("tpm") == 1200000 and ll5.get("rpm") == 5000 and ll5.get("source") == "success-header")

# ── (6) ANTI-TRAP A (latest-wins): a new observation OVERWRITES a transient reading, never min-latches ──
print("-- (6) latest-wins: a fresh observation overwrites a prior (transient) learned limit --")
Vw = "flapvendor"
dispatch.learn_rate_limit(Vw, tpm=500, source="429-header")       # a transient LOW reading
dispatch.learn_rate_limit(Vw, tpm=2000000, source="success-header")  # the next normal call sees the real limit
ck("a later observation overwrites the earlier (self-heals an intermittent low reading)",
   dispatch.learned_limits(Vw).get("tpm") == 2000000)

# ── (7) ANTI-TRAP C (cooldown cap): a bad/huge Retry-After can't wedge us in an endless cooldown ──
print("-- (7) cooldown cap: a huge Retry-After is honored but CAPPED (dispatch.cooldown_cap_s) --")
os.environ["SPENDGUARD_DISPATCH_COOLDOWN_CAP_S"] = "300"
dispatch.learn_rate_limit("wedgevendor", retry_after_s=86400)     # a misbehaving 'Retry-After: 1 day'
left = dispatch._cooldown_left("wedgevendor")
ck("a 86400s Retry-After is capped to <= cooldown_cap_s (300s), not honored whole", 0 < left <= 300)
os.environ.pop("SPENDGUARD_DISPATCH_COOLDOWN_CAP_S", None)

print(f"\n{'[FAIL]' if _fails else 'OK'} test_rate_limit_selfcalibrate: {len(_fails)} failure(s)")
sys.exit(1 if _fails else 0)

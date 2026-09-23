"""Guard — UNIVERSAL ADMISSION: a plain LABELLED adapters.call is PACED through the dispatch governor, not just the
governed=True / bulk_delegate fans. This is what makes 'with a managed queue, no user ever sees a 429' real — without
it, a serial caller (or a consumer's own thread pool) blows a provider's tokens/minute ceiling because nothing gated it.

  (1) a serial labelled call enters dispatch.admit — ONCE, with shed=False (a lone call is never silently moved to the
      paid API to escape a busy lane) and a positive est_tokens (input+output, for TPM pacing);
  (2) a call ALREADY holding a dispatch slot (a bulk_delegate runner acquired first) does NOT re-admit — one admission
      per logical call;
  (3) governed=True still admits with shed=True (the fan wants throughput — unchanged);
  (4) an UNLABELLED call (no intent/sig) is not managed-admitted;
  (5) dispatch.manage_all OFF → a serial call is not admitted (the kill switch works).
Hermetic: dispatch.admit + _call_guarded + the resolver stubbed; routing off; no network."""
import os
import sys
import tempfile

os.environ.setdefault("SPENDGUARD_TEST_ISOLATED", "1")
os.environ.setdefault("SPENDGUARD_HOME", tempfile.mkdtemp(prefix="spendguard-adm-"))
os.environ["SPENDGUARD_ROUTE_THROUGH_QUEUE"] = "0"   # isolate: don't also route these probe calls through the queue

from spendguard import adapters, dispatch

_fails = []
def ck(label, cond):
    if not cond:
        _fails.append(label)
    print(f"  [{'OK' if cond else 'FAIL'}] {label}")

_admits = []
_saved_admit = dispatch.admit
def _cap_admit(vendor, model, deadline_s, no_metered_fallback=False, est_tokens=0, shed=True, skip_lane=False):
    _admits.append({"vendor": vendor, "model": model, "shed": shed, "est_tokens": est_tokens, "skip_lane": skip_lane})
    return dispatch._Admission(True, False, False, vendor, model, skip_lane)   # ok, not shed, not held (no real slot)
dispatch.admit = _cap_admit

_saved_guarded = adapters._call_guarded
adapters._call_guarded = lambda model, prompt, **kw: {
    "text": "ok", "model": model, "provider": "x", "cost": 0.0, "in_tok": 1, "out_tok": 1, "error": None}
adapters._resolve_guard.on = True                    # skip the served-substitute resolver (no network)

try:
    # ── (1) serial labelled call → admits ONCE, shed=False, est_tokens>0 ──
    print("-- (1) a plain labelled adapters.call is admitted (paced), shed=False, est_tokens>0 --")
    _admits.clear()
    adapters.call("openai:gpt-x", "hello world " * 100, sig="adm:test")
    ck("serial labelled call entered dispatch.admit exactly once", len(_admits) == 1)
    ck("serial admission is shed=False (a lone call never sheds $0-lane → paid)", bool(_admits) and _admits[0]["shed"] is False)
    ck("est_tokens is a positive input+output estimate", bool(_admits) and _admits[0]["est_tokens"] > 0)
    ck("a non-metered_only serial call keys on lane-or-vendor (skip_lane=False)", bool(_admits) and _admits[0]["skip_lane"] is False)

    # ── (1b) a metered_only serial call gates on the metered VENDOR bucket (skip_lane=True) so it gets RPM/TPM ──
    print("-- (1b) a metered_only serial call admits with skip_lane=True (paced on the paid vendor's RPM/TPM) --")
    _admits.clear()
    adapters.call("openai:gpt-x", "p", sig="adm:test", metered_only=True)
    ck("metered_only serial call admits with skip_lane=True", len(_admits) == 1 and _admits[0]["skip_lane"] is True)

    # ── (2) already holding a dispatch slot → NO second admission ──
    print("-- (2) a call already inside an outer dispatch slot (bulk runner) does not re-admit --")
    _admits.clear()
    dispatch._held().append(None)                    # simulate an outer acquire() on THIS thread (holding() → True)
    try:
        adapters.call("openai:gpt-x", "p", sig="adm:test")
        ck("holding an outer slot → managed admission skipped (one admission per logical call)", len(_admits) == 0)
    finally:
        dispatch._pop_held()

    # ── (3) governed=True → admits with shed=True (unchanged) ──
    print("-- (3) governed=True still admits with shed=True (a fan wants shed-to-metered throughput) --")
    _admits.clear()
    adapters.call("openai:gpt-x", "p", sig="adm:test", governed=True)
    ck("governed=True admits with shed=True", len(_admits) == 1 and _admits[0]["shed"] is True)

    # ── (4) unlabelled call → not managed-admitted ──
    print("-- (4) an UNLABELLED call (no intent/sig) is not managed-admitted --")
    _admits.clear()
    adapters.call("openai:gpt-x", "p")
    ck("unlabelled call → no admission (managed admission needs a label)", len(_admits) == 0)

    # ── (5) manage_all OFF → serial call not admitted ──
    print("-- (5) dispatch.manage_all OFF → serial admission disabled (kill switch) --")
    _admits.clear()
    os.environ["SPENDGUARD_DISPATCH_MANAGE_ALL"] = "0"
    try:
        adapters.call("openai:gpt-x", "p", sig="adm:test")
        ck("manage_all off → serial call not admitted", len(_admits) == 0)
    finally:
        os.environ.pop("SPENDGUARD_DISPATCH_MANAGE_ALL", None)
finally:
    dispatch.admit = _saved_admit
    adapters._call_guarded = _saved_guarded
    adapters._resolve_guard.on = False

print(f"\n{'[FAIL]' if _fails else 'OK'} test_universal_admission: {len(_fails)} failure(s)")
sys.exit(1 if _fails else 0)

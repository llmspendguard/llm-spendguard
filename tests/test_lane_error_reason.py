"""Every lane MISS row carries a STRUCTURED reason code (a closed vocabulary), not just a prose `error`.

The ask: "add the structured reason on lane error rows ... make sure these are valid." A caller degrading a lane
fan to Batch (refuse_billed=True → split served/unserved → batch the remainder) must route the UNSERVED set by
CAUSE — retry lanes (dispatch/quota), send to batch (empty/shape_miss/lane_error/arity_miss), or investigate
(call_raised/api_error) — WITHOUT string-sniffing the human `error`. This guard asserts, on the real bulk_delegate
row-builder (lane machinery + adapters.call stubbed; no network, no spend):
  • each miss path sets the RIGHT reason from the closed vocabulary,
  • a SERVED row (has text) has reason=None — a reason is a miss signal, never noise on a success,
  • NO error row (text is None) is ever reason-less,
  • a DELIBERATE stop (DispatchTimeout admission shed / a refusal) from the governor HALTS the fan — it is NOT
    downgraded to a 'dispatch' row (the refusal-containment doctrine; mirrors the vision runner).
Vision-specific reason codes (no_image/image_too_big/…) are asserted in test_bulk_vision_fan.py.
"""
import os
import sys
import tempfile

os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="sg-lanereason-")
os.environ.setdefault("SPENDGUARD_TEST_ISOLATED", "1")
os.environ.setdefault("SPENDGUARD_NO_AUTOINSTALL", "1")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from spendguard import (adapters, lane_balance, lane_catalog, lane_bandit, lane_economics,
                        dispatch as _dispatch)
from spendguard.dispatch import DispatchTimeout

fails = []


def ck(name, cond):
    print(("  [OK] " if cond else "  [FAIL] ") + name)
    if not cond:
        fails.append(name)


# ── one good arm, governor open, cooling off — the minimal harness so bulk_delegate runs exactly one task on a lane ──
lane_catalog.arms = lambda flt=None: [("codex", "gpt-5.6-luna")]
lane_catalog.lane_provider = lambda l: "openai"
lane_bandit._arm_cooling = lambda l, u: False
lane_bandit.arm_stats = lambda intent: {("codex", "gpt-5.6-luna"): {"winrate": 1.0, "trials": 2}}
lane_economics.prompt_lane_reserved = lambda lane: False
adapters._lane_cooling = lambda ln: False
_dispatch.release = lambda *a, **k: None

# The closed vocabulary a caller may switch on. A reason OUTSIDE this set is a silent contract break.
ALLOWED = {None, "dispatch", "call_raised", "empty", "shape_miss", "lane_error", "quota",
           "arity_miss", "arity_check_error", "api_error",
           "no_vision_model", "no_image", "image_too_big", "image_unreadable"}

_SERVED = {"text": "ok", "parsed": None, "cost": 0.0, "executor": "codex",
           "provider": "openai", "model": "gpt-5.6-luna", "error": None}


def _acquire_ok(*a, **k):
    return 0.0


def row_for(call_result_or_exc, *, acquire=_acquire_ok, expect_ids=None):
    """Run ONE task through the real bulk_delegate row-builder with adapters.call / dispatch.acquire stubbed."""
    _dispatch.acquire = acquire
    if isinstance(call_result_or_exc, BaseException):
        def _c(model, prompt, **kw):
            raise call_result_or_exc
    else:
        def _c(model, prompt, **kw):
            return dict(call_result_or_exc)
    adapters.call = _c
    rows = lane_balance.bulk_delegate(["task-0"], "lane-reason:probe", force=True, expect_ids=expect_ids)
    return rows[0]


# ── SERVED: a real answer → reason is None (a reason is a MISS signal, never noise on a success) ──
r = row_for(_SERVED)
ck("SERVED row (has text) → reason is None", r.get("text") == "ok" and r.get("reason") is None)

# ── adapters MISS reasons propagate onto the row verbatim (refuse_billed path: text=None + reason + error) ──
for code in ("empty", "shape_miss", "lane_error", "quota"):
    r = row_for({"text": None, "cost": None, "executor": "codex", "provider": "openai",
                 "model": "gpt-5.6-luna", "reason": code, "error": f"refused: {code}"})
    ck(f"adapters miss reason '{code}' → row.reason == '{code}'", r.get("reason") == code and r.get("text") is None)

# ── api_error: refuse_billed=False and the METERED fallback failed → adapters gives an error but NO reason ──
r = row_for({"text": None, "cost": 0.0, "executor": "api", "provider": "openai",
             "model": "gpt-5.6-luna", "error": "HTTP 500 from provider"})
ck("metered-fallback failure (error, no adapters reason) → row.reason == 'api_error'",
   r.get("reason") == "api_error" and r.get("text") is None)

# ── call_raised: adapters.call itself raises (it normally returns an error dict) ──
r = row_for(ValueError("adapters exploded"))
ck("adapters.call raises → row.reason == 'call_raised'", r.get("reason") == "call_raised" and r.get("text") is None)

# ── dispatch: a NON-deliberate governor error becomes a 'dispatch' row (retryable, not a shape/quota miss) ──
def _acquire_boom(*a, **k):
    raise RuntimeError("governor backend down")


r = row_for(_SERVED, acquire=_acquire_boom)
ck("non-deliberate governor error → row.reason == 'dispatch'", r.get("reason") == "dispatch" and r.get("text") is None)

# ── arity_miss: a shape-perfect but INCOMPLETE envelope (dropped ids) → a MISS with reason 'arity_miss' ──
r = row_for({**_SERVED, "text": '{"results":[]}'}, expect_ids=lambda t: ["id1", "id2"])
ck("incomplete envelope (ids dropped) → row.reason == 'arity_miss'",
   r.get("reason") == "arity_miss" and r.get("text") is None)

# ── arity_check_error: the completeness check ITSELF broke → fail CLOSED (a MISS, retried; never a swallowed pass) ──
def _boom_ids(task):
    raise RuntimeError("expect_ids blew up")


r = row_for({**_SERVED, "text": '{"results":[{"id":"id1"}]}'}, expect_ids=_boom_ids)
ck("completeness check error → row.reason == 'arity_check_error' (fail closed)",
   r.get("reason") == "arity_check_error" and r.get("text") is None)

# ── DELIBERATE STOP: a governor shed (DispatchTimeout) HALTS the fan — it is NOT downgraded to a 'dispatch' row ──
def _acquire_shed(*a, **k):
    raise DispatchTimeout("no slot within deadline")


halted = False
try:
    row_for(_SERVED, acquire=_acquire_shed)
except DispatchTimeout:
    halted = True
ck("a DispatchTimeout (deliberate stop) PROPAGATES out of bulk_delegate — never a swallowed row", halted)

# ── the whole vocabulary is closed: every reason produced above is a known code (no silent contract break) ──
_seen = set()
for res, eids in ((_SERVED, None),
                  ({"text": None, "reason": "empty", "error": "x"}, None),
                  ({"text": None, "error": "HTTP 500"}, None),
                  (ValueError("boom"), None),
                  ({**_SERVED, "text": '{"results":[]}'}, lambda t: ["id1"])):
    _seen.add(row_for(res, expect_ids=eids).get("reason"))
ck("every reason emitted is in the closed ALLOWED vocabulary", _seen <= ALLOWED)

print(("[OK]" if not fails else "[FAIL]") + " lane error reason: %d failure(s)" % len(fails))
sys.exit(1 if fails else 0)

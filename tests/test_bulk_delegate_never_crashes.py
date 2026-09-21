"""GUARD — bulk_delegate NEVER crashes its caller on dispatch saturation, and NEVER buries a spend refusal.

The failure this pins (measured 2026-09-21): a saturated kimi-code lane made dispatch raise DispatchTimeout out of a
532-task honestreview review fan (repo_review_panel → bulk_delegate → _run_task_on_api, the PINNED per-vendor panel
runner), and it propagated out of bulk_delegate and killed the whole honestreview process. The fix routes admission
through dispatch.acquire_or_none() — acquire(), but a queue-slot TIMEOUT returns None instead of raising — so a
saturated lane becomes a per-task MISS row; a belt-and-suspenders in the result loop contains any UNEXPECTED per-task
raise; and a genuine SPEND REFUSAL (SpendGateRefused) still PROPAGATES (refusal-containment). Three claims:

  (a) acquire_or_none() returns None (no slot within the deadline) → EVERY task is a MISS row (reason=dispatch_saturated)
      and bulk_delegate RETURNS — the exact crash, now a graceful miss the caller's on_miss/queue handles;
  (b) an UNEXPECTED per-task exception (raised OUTSIDE a runner's own try) → contained as a task_crashed miss row by
      the result-loop belt, the fan still RETURNS — 'no crashes EVER', not just 'no DispatchTimeout crash';
  (c) a genuine SpendGateRefused from the call still HALTS the fan (bulk_delegate re-raises) — money-refusal
      containment is preserved, never downgraded to 'keep going'.

For (a) and (b) the test calls bulk_delegate DIRECTLY: a raise escaping it would abort THIS test with a traceback
(a loud failure — exactly the regression the guard exists to catch), so no catch-all is needed or wanted; only (c)
narrowly catches SpendGateRefused, the type it is asserting propagates. Hermetic: dispatch.acquire_or_none /
adapters.call / provider_for / calls.set_context are stubbed; a synthetic model id (never dialed); isolated home; no
network, no spend. Drives the PINNED runner (model_for callable → _run_task_on_api) — the exact path that crashed.
"""
import os
import sys
import tempfile

os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="sg-crashproof-")
os.environ.setdefault("SPENDGUARD_TEST_ISOLATED", "1")
os.environ.setdefault("SPENDGUARD_NO_AUTOINSTALL", "1")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from spendguard import lane_balance, adapters, calls, dispatch   # noqa: E402
from spendguard.gate import SpendGateRefused                     # noqa: E402

fails = []


def ck(name, cond):
    print(("  [OK] " if cond else "  [FAIL] ") + name)
    if not cond:
        fails.append(name)


INTENT = "test-crashproof"
STUB_MODEL = "acme:stub-model"        # synthetic — adapters.call is stubbed, so this id is only ever split, never dialed
TASKS = ["task-1", "task-2", "task-3"]


def _served_call(model, prompt, **kw):
    return {"text": "an answer", "provider": "acme", "model": "stub-model", "executor": "api",
            "cost": 0.0, "in_tok": 5, "out_tok": 3, "error": None, "reason": None}


def _run():
    # a PINNED fan (model_for callable → the _run_task_on_api path, the exact crash site). checkpoint/on_miss default.
    return lane_balance.bulk_delegate(list(TASKS), INTENT, model_for=lambda t: STUB_MODEL,
                                      deadline_s=5.0, max_workers=3)


_saved = (adapters.call, adapters.provider_for, calls.set_context, dispatch.acquire_or_none)
_provider_for = lambda m: (m or "").split(":", 1)[0]
adapters.provider_for = _provider_for
calls.set_context = lambda **k: None

try:
    # ── (a) full saturation: acquire_or_none returns None for every task → all MISS rows, bulk_delegate RETURNS ──
    #      A raise here would abort the test (a loud failure) — that IS the regression this guard catches, so the
    #      call is direct and uncaught by design.
    print("-- (a) dispatch saturation (acquire_or_none → None) → per-task MISS rows, no crash --")
    dispatch.acquire_or_none = lambda *a, **k: None
    adapters.call = _served_call
    res = _run()
    ck("bulk_delegate RETURNED a row per task on full saturation (did not raise)",
       isinstance(res, list) and len(res) == len(TASKS))
    ck("every row is a MISS (text=None) — a saturated vendor never fakes a served answer",
       bool(res) and all((r or {}).get("text") is None for r in res))
    ck("every miss is reason=dispatch_saturated (a queue timeout, distinct from a model/shape miss)",
       bool(res) and all((r or {}).get("reason") == "dispatch_saturated" for r in res))

    # ── (b) an UNEXPECTED per-task exception (raised OUTSIDE a runner try) → contained by the belt, still returns ──
    print("\n-- (b) an unexpected per-task raise is contained by the result-loop belt, never crashes the fan --")
    dispatch.acquire_or_none = lambda *a, **k: 0.0    # admission succeeds; the raise comes from elsewhere in the runner

    def _boom(_m):
        raise RuntimeError("simulated unexpected failure before the guarded call")

    adapters.provider_for = _boom                      # provider_for runs BEFORE the runner's try → escapes → belt
    adapters.call = _served_call
    res = _run()
    ck("bulk_delegate RETURNED a row per task despite an unexpected per-task raise (belt-and-suspenders)",
       isinstance(res, list) and len(res) == len(TASKS))
    ck("the unexpected raise became a task_crashed MISS row, not a crash",
       bool(res) and all((r or {}).get("reason") == "task_crashed" and (r or {}).get("text") is None for r in res))
    adapters.provider_for = _provider_for              # restore for (c)

    # ── (c) a genuine SPEND REFUSAL still HALTS the fan (refusal-containment preserved) ──
    print("\n-- (c) a genuine SpendGateRefused still propagates (money-refusal containment) --")
    dispatch.acquire_or_none = lambda *a, **k: 0.0

    def _refuse(model, prompt, **kw):
        raise SpendGateRefused("over cap — deliberate refusal")

    adapters.call = _refuse
    propagated = False
    try:
        _run()
    except SpendGateRefused:
        propagated = True                              # the ONLY catch: the exact type this claim asserts propagates
    ck("a SpendGateRefused from the call PROPAGATES out of bulk_delegate (never buried as a miss)", propagated)
finally:
    adapters.call, adapters.provider_for, calls.set_context, dispatch.acquire_or_none = _saved

print(f"\n{'[FAIL]' if fails else 'OK'} test_bulk_delegate_never_crashes: {len(fails)} failure(s)")
sys.exit(1 if fails else 0)

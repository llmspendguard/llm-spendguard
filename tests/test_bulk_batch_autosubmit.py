"""Guard — bulk_delegate's DEFAULT batch leg wires the batch DECISION to a REAL submission. When on_miss="batch" is
given NO explicit batch_submit but a batch model is resolvable (`batch_model=` arg, or config advisor.batch_model),
the overflow miss set is submitted ONCE to the OpenAI Batch API via submit.submit_chat_tasks. Pins (always-missing
lane, submit_chat_tasks stubbed — no lanes, no network, no spend):
  · batch_model= arg → the whole miss set + that model reach submit_chat_tasks ONCE, rows become queued_batch with
    the returned batch_id; intent is forwarded;
  · config advisor.batch_model (no arg) drives the same default;
  · batch_cap= arg reaches submit_chat_tasks as cap_dollars;
  · submit_chat_tasks returning a TYPED error → the misses stay batch_eligible (never falsely queued);
  · NO batch model anywhere → submit_chat_tasks is NEVER called, misses batch_eligible (an uncapped auto-submit never
    happens by omission);
  · a DELIBERATE spend-stop from submit_chat_tasks PROPAGATES out of bulk_delegate.
Offline: arms / adapters.call / dispatch monkeypatched (mirrors test_bulk_delegate_on_miss)."""
import os
import sys
import tempfile

os.environ.setdefault("SPENDGUARD_TEST_ISOLATED", "1")
os.environ.setdefault("SPENDGUARD_HOME", tempfile.mkdtemp(prefix="spendguard-autobatch-"))

from spendguard import lane_balance, lane_catalog, lane_economics, adapters, dispatch, calls, gate, submit, config

_fails = []
def ck(label, cond):
    if not cond:
        _fails.append(label)
    print(f"  [{'OK' if cond else 'FAIL'}] {label}")

# ── a lane that always MISSES (adapters.call returns an error row, no text) — nothing bills ──
lane_balance._bulk_arms = lambda intent, lanes=None: [("fakelane", "fakemodel")]
lane_catalog.lane_provider = lambda ln: "fakeprov"
lane_economics.prompt_lane_reserved = lambda ln: False
adapters._lane_cooling = lambda ln: False
dispatch.acquire = lambda *a, **k: None
dispatch.release = lambda *a, **k: None
calls.set_context = lambda *a, **k: None
adapters.call = lambda *a, **k: {"error": "lane miss", "reason": "quota"}       # no text → a miss

TASKS = [{"id": "a"}, {"id": "b"}, {"id": "c"}]

# ── stub the batch submitter: record every call, return a configurable result / raise ──
_ctl = {"result": {"batch_id": None, "error": None}, "raise": None}
_calls = []
def _stub_submit_chat(tasks, model, *, system=None, schema=None, intent=None, cap_dollars=None, **kw):
    _calls.append({"tasks": list(tasks), "model": model, "cap": cap_dollars, "intent": intent})
    if _ctl["raise"] is not None:
        raise _ctl["raise"]
    return dict(_ctl["result"])
submit.submit_chat_tasks = _stub_submit_chat

print("-- batch_model= arg: the overflow is submitted ONCE via submit_chat_tasks, rows queued_batch --")
_calls.clear(); _ctl["raise"] = None; _ctl["result"] = {"batch_id": "batch_AUTO", "error": None}
rows = lane_balance.bulk_delegate(TASKS, "myintent", on_miss="batch", batch_model="gpt-5-nano", force=True)
ck("submit_chat_tasks called exactly ONCE (one batch for the remainder)", len(_calls) == 1)
ck("the whole miss set was handed to it", len(_calls[0]["tasks"]) == 3)
ck("the batch model is the batch_model= arg", _calls[0]["model"] == "gpt-5-nano")
ck("intent forwarded to the submitter", _calls[0]["intent"] == "myintent")
ck("every missed row is queued_batch with the returned batch_id",
   all(r.get("reason") == "queued_batch" and r.get("batch") == "batch_AUTO" for r in rows))

print("-- config advisor.batch_model drives the default submit when no arg is passed --")
_calls.clear(); _ctl["result"] = {"batch_id": "batch_CFG", "error": None}
_orig_cfg = config._cfg_get
config._cfg_get = lambda s, k, d=None: ("gpt-5-nano" if (s, k) == ("advisor", "batch_model") else _orig_cfg(s, k, d))
try:
    rows2 = lane_balance.bulk_delegate(TASKS, "myintent", on_miss="batch", force=True)
finally:
    config._cfg_get = _orig_cfg
ck("config-resolved model drives submit (no batch_model arg needed)",
   len(_calls) == 1 and _calls[0]["model"] == "gpt-5-nano")
ck("rows queued_batch via the config-resolved model", all(r.get("reason") == "queued_batch" for r in rows2))

print("-- batch_cap= arg reaches submit_chat_tasks as cap_dollars --")
_calls.clear(); _ctl["result"] = {"batch_id": "batch_CAP", "error": None}
lane_balance.bulk_delegate(TASKS, "myintent", on_miss="batch", batch_model="gpt-5-nano", batch_cap=2.5, force=True)
ck("batch_cap forwarded as cap_dollars", bool(_calls) and _calls[0]["cap"] == 2.5)

print("-- a TYPED submit error → misses stay batch_eligible (never falsely queued) --")
_calls.clear(); _ctl["raise"] = None; _ctl["result"] = {"batch_id": None, "error": "submit endpoint down"}
rows3 = lane_balance.bulk_delegate(TASKS, "myintent", on_miss="batch", batch_model="gpt-5-nano", force=True)
ck("submit_chat_tasks WAS attempted", len(_calls) == 1)
ck("a typed submit error → misses batch_eligible, never queued_batch",
   all(r.get("reason") == "batch_eligible" for r in rows3))
ck("no batch handle on error", all("batch" not in r for r in rows3))

print("-- NO batch model anywhere → submit_chat_tasks NEVER called, misses batch_eligible (no auto-submit by omission) --")
_calls.clear(); _ctl["result"] = {"batch_id": "X", "error": None}
rows4 = lane_balance.bulk_delegate(TASKS, "myintent", on_miss="batch", force=True)   # no arg; isolated config → None
ck("no batch model → submit_chat_tasks never called", len(_calls) == 0)
ck("misses batch_eligible (an uncapped auto-submit never happens by omission)",
   all(r.get("reason") == "batch_eligible" for r in rows4))

print("-- a DELIBERATE spend-stop from submit_chat_tasks PROPAGATES out of bulk_delegate --")
_calls.clear(); _ctl["result"] = {"batch_id": None, "error": None}
_ctl["raise"] = gate.SpendGateRefused.__new__(gate.SpendGateRefused)
try:
    lane_balance.bulk_delegate(TASKS, "myintent", on_miss="batch", batch_model="gpt-5-nano", force=True)
    ck("deliberate stop propagated", False)
except gate.SpendGateRefused:
    ck("deliberate stop from the submitter propagates out of bulk_delegate", True)
_ctl["raise"] = None

print(f"\n{'[FAIL]' if _fails else 'OK'} test_bulk_batch_autosubmit: {len(_fails)} failure(s)")
sys.exit(1 if _fails else 0)

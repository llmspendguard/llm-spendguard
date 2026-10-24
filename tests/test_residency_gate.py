"""residency_gate — the PostToolUse context-residency NUDGE (Review 1, #3b). Offline: the $0-lane verdict call, the
session turn/model lookup, and the rates are stubbed. Zero real spend.

Pins the contract: a MECHANICAL size+turn gate decides whether to consider a result; the AGENTIC verdict (one call on
a READY lane, CONTENT-HASH cached) decides delegability; the output WARNS (systemMessage) and NEVER blocks; a small
result or an early session stays SILENT (both directions, per no-shortcuts); and the judgement is ONE gated call —
never a subagent/session spawn (the symgrep $70/day failure mode)."""
import os
import sys
import tempfile

os.environ["SPENDGUARD_TEST_ISOLATED"] = "1"
os.environ["SPENDGUARD_NO_AUTOINSTALL"] = "1"
os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="sg-resgate-")
os.environ["SPENDGUARD_RESIDENCY_MIN_RESULT_BYTES"] = "100"
os.environ["SPENDGUARD_RESIDENCY_MIN_TURNS"] = "10"
os.environ["SPENDGUARD_RESIDENCY_MIN_RESIDENT_TOKENS"] = "1000"
os.environ["SPENDGUARD_RESIDENCY_REMAINING_TURNS_EST"] = "100"
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from spendguard import residency_gate as rg, adapters, plan_admission, claudecode, pricing  # noqa: E402

fails = []
from functools import partial
from _script_checks import record_script_check
ck = partial(record_script_check, fails)

_calls = []
def _fake_call(model, prompt, **kw):
    _calls.append((model, prompt, kw))
    return {"ok": True}
adapters.call = _fake_call
_VERDICT = {"delegable": True, "why": "a log read to extract one number"}
adapters.structured_reply = lambda r: _VERDICT
plan_admission.ready_meta_model = lambda m: "gpt-5.6-luna"       # a READY $0-lane model, never the capped plan
claudecode.context_trajectory = lambda cid: {"turns": 50, "current": 500, "model": "claude-opus-4-8"}
claudecode._cache_read_rate = lambda m: 1e-6
pricing.price = lambda m: {"in_": 3.0}
pricing.realtime_cost = lambda model, in_tok, out_tok, cached_in_tok=0, cache_creation_tok=0, **kw: (
    cached_in_tok * 0.000001 + cache_creation_tok * 0.000003 + out_tok * 0.000015)

_big = {"tool_name": "Bash", "session_id": "S1", "tool_input": {"command": "cat big.log"},
        "tool_response": "Y" * 4000}

# ── fires on a large result in a long session, judged delegable → a WARNING naming the cost, never a block ──
_calls.clear()
out = rg.evaluate_posttooluse(_big)
ck("a large delegable result in a long session fires a systemMessage", bool(out.get("systemMessage")))
ck("the warning names residency + the delegable reason", "residency" in out["systemMessage"] and "log read" in out["systemMessage"])
ck("the warning names a resident-vs-subagent cost", "re-read" in out["systemMessage"] and "subagent" in out["systemMessage"])
ck("it is a WARNING, not a block (no permissionDecision=deny)",
   "permissionDecision" not in str(out) and out.get("hookSpecificOutput", {}).get("hookEventName") == "PostToolUse")
ck("exactly ONE gated verdict call — never a session/subagent spawn", len(_calls) == 1)
ck("judge is minimal, strict-schema, and output-bounded",
   _calls[0][2].get("reasoning") == "minimal" and _calls[0][2].get("max_tokens") == 120
   and _calls[0][2].get("schema", {}).get("additionalProperties") is False
   and _calls[0][2].get("no_metered_fallback") is True)
ck("judge receives the whole tool result", "Y" * 4000 in _calls[0][1])

# ── content-hash cache: the same result is judged ONCE ──
rg.evaluate_posttooluse(_big)
ck("the verdict is content-hash cached (no second verdict call)", len(_calls) == 1)

# ── silent below the size gate ──
_calls.clear()
small = {**_big, "tool_input": {"command": "echo hi"}, "tool_response": "ok"}
ck("a small result is SILENT (below the size gate)", rg.evaluate_posttooluse(small) == {} and _calls == [])

# ── silent in an early session (turn gate), even for a large result ──
_calls.clear()
claudecode.context_trajectory = lambda cid: {"turns": 2, "current": 500, "model": "claude-opus-4-8"}
ck("a large result in an EARLY session is SILENT (turn gate)", rg.evaluate_posttooluse({**_big, "session_id": "S2"}) == {})
ck("no verdict call wasted on an early-session result", _calls == [])
claudecode.context_trajectory = lambda cid: {"turns": 2, "current": 2000, "model": "claude-opus-4-8"}
_calls.clear()
ck("resident-context threshold can satisfy the long-session gate without many turns",
   bool(rg.evaluate_posttooluse({**_big, "session_id": "S-resident"}).get("systemMessage")))
ck("the resident-context path reuses the content verdict", len(_calls) == 0)
claudecode.context_trajectory = lambda cid: {"turns": 50, "current": 500, "model": "claude-opus-4-8"}

# ── every threshold/horizon is environment-configured, and the kill switch is a total no-op ──
os.environ["SPENDGUARD_RESIDENCY_MIN_RESULT_BYTES"] = "5000"
_calls.clear()
ck("size threshold override changes gating", rg.evaluate_posttooluse({**_big, "tool_response": "W" * 4000}) == {}
   and _calls == [])
os.environ["SPENDGUARD_RESIDENCY_MIN_RESULT_BYTES"] = "100"

os.environ["SPENDGUARD_RESIDENCY_REMAINING_TURNS_EST"] = "2"
two_turn_warning = rg.evaluate_posttooluse({**_big, "tool_response": "H" * 4000}).get("systemMessage", "")
os.environ["SPENDGUARD_RESIDENCY_REMAINING_TURNS_EST"] = "200"
two_hundred_turn_warning = rg.evaluate_posttooluse({**_big, "tool_response": "I" * 4000}).get("systemMessage", "")
ck("remaining-turn horizon override changes the pricing-derived warning",
   two_turn_warning and two_hundred_turn_warning and two_turn_warning != two_hundred_turn_warning)

os.environ["SPENDGUARD_RESIDENCY_NUDGE"] = "0"
_calls.clear()
ck("kill switch is a total no-op", rg.evaluate_posttooluse({**_big, "tool_response": "K" * 4000}) == {}
   and _calls == [])
os.environ["SPENDGUARD_RESIDENCY_NUDGE"] = "1"

# ── any internal failure is silent success; it never warns or blocks the tool ──
original_trajectory = claudecode.context_trajectory
claudecode.context_trajectory = lambda cid: (_ for _ in ()).throw(OSError("synthetic unreadable transcript"))
ck("unreadable transcript fails open without a warning", rg.evaluate_posttooluse(_big) == {})
claudecode.context_trajectory = original_trajectory
original_structured_reply = adapters.structured_reply
adapters.structured_reply = lambda result: (_ for _ in ()).throw(RuntimeError("synthetic judge failure"))
ck("judge failure fails open without a warning", rg.evaluate_posttooluse({**_big, "tool_response": "J" * 4000}) == {})
adapters.structured_reply = original_structured_reply

# ── a NON-delegable result (belongs in the thread) is SILENT ──
_calls.clear()
_VERDICT = {"delegable": False, "why": "a file about to be edited"}
_nondelegable = {**_big, "session_id": "S3", "tool_response": "Z" * 4000}
ck("a result judged non-delegable is SILENT (no false alarm)", rg.evaluate_posttooluse(_nondelegable) == {})

# ── fail-open: a malformed payload never raises, returns silent ──
ck("a malformed payload degrades to silent (fail-open)", rg.evaluate_posttooluse({"tool_response": None}) == {})

print(f"\n{'[FAIL]' if fails else 'OK'} test_residency_gate: {len(fails)} failure(s)")
sys.exit(1 if fails else 0)

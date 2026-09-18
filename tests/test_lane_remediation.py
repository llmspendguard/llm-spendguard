"""Guard — reliability.remediate turns a sweep's down lanes/providers into agentic, CACHED fixes.

The routine health check ('which lane needs a login?') must: classify each UNREACHABLE resource agentically (what
an error means + how to fix it is a judgement), send the WHOLE error (never truncated), CACHE by failure signature
(so a scheduled check is $0 for a known failure, paying only for a NEW one), and return [] when all healthy.
Offline: the meta LLM call is stubbed — no network, no spend."""
import os, sys, tempfile

if not os.environ.get("SPENDGUARD_TEST_ISOLATED"):
    os.environ["SPENDGUARD_TEST_ISOLATED"] = "1"
    os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="spendguard-remed-")
    _self = os.path.realpath(__file__)
    _root = os.path.realpath(os.path.dirname(__file__)) + os.sep
    if not _self.startswith(_root):
        raise SystemExit("refusing to re-exec a path outside the test directory: %s" % _self)
    os.execv(sys.executable, [sys.executable, _self])

from spendguard import reliability, adapters, calls, config

_fails = []
def ck(label, cond):
    if not cond:
        _fails.append(label)
    print(f"  [{'OK' if cond else 'FAIL'}] {label}")

# stub the meta LLM: record what it was asked, return a canned remediation
_seen = {"n": 0, "prompt": None}
def _fake_call(model, prompt, **kw):
    _seen["n"] += 1
    _seen["prompt"] = prompt
    return {"parsed": {"issue": "OAuth session expired", "fix": "re-login the claude CLI", "command": "claude setup-token"},
            "cost": 0.0, "error": None}   # 'parsed' is the field the adapter surfaces its fence-tolerant decode under
adapters.call = _fake_call
calls.context = lambda **k: __import__("contextlib").nullcontext()
config.advisor_model = lambda: "stub-model"

LONG_ERR = '{"is_error":true,"result":"Failed to authenticate: OAuth session expired and could not be refreshed"}' + (" x" * 400)
SWEEP = {"lanes": {"claude-code": {"reachable": False, "reason": LONG_ERR},
                   "codex": {"reachable": True, "reason": None}},
         "metered": {"openai": {"reachable": True}, "gemini": {"reachable": True}}}

print("-- remediate classifies each DOWN resource agentically, healthy ones ignored --")
acts = reliability.remediate(SWEEP)
ck("one action (only the down lane)", len(acts) == 1 and acts[0]["resource"] == "claude-code")
ck("carries the agentic issue/fix/command", acts[0]["issue"] == "OAuth session expired"
   and acts[0]["command"] == "claude setup-token")
ck("the WHOLE error was sent to the classifier (not truncated)", LONG_ERR in (_seen["prompt"] or ""))
ck("the LLM was called once", _seen["n"] == 1)

print("-- CACHED by signature: the same failure re-remediates for $0 (no second LLM call) --")
acts2 = reliability.remediate(SWEEP)
ck("same fix returned", acts2 and acts2[0]["command"] == "claude setup-token")
ck("cache hit — NO new LLM call", _seen["n"] == 1)
ck("marked cached", acts2[0].get("cached") is True)

print("-- a DIFFERENT failure class pays once (new signature) --")
SWEEP2 = {"lanes": {"gemini": {"reachable": False, "reason": "invalid model gemini-old-x"}}, "metered": {}}
reliability.remediate(SWEEP2)
ck("a new failure class triggered a new LLM call", _seen["n"] == 2)

print("-- all healthy → [] (a routine check is $0 when nothing is wrong) --")
_seen["n"] = 0
healthy = reliability.remediate({"lanes": {"codex": {"reachable": True}}, "metered": {"openai": {"reachable": True}}})
ck("no actions when all healthy", healthy == [])
ck("no LLM call when all healthy", _seen["n"] == 0)

print("-- signature is EXACT identity: identical errors match, ANY difference re-classifies (no fuzzy same-class) --")
s1 = reliability._remediation_signature("lane", "claude-code", "OAuth session expired")
s1b = reliability._remediation_signature("lane", "claude-code", "OAuth session expired")
s2 = reliability._remediation_signature("lane", "claude-code", "credits depleted")
ck("identical (kind,resource,error) → same signature (cache hit)", s1 == s1b)
ck("a DIFFERENT error → different signature (never a wrong-class cached fix)", s1 != s2)

print(f"\n{'[FAIL]' if _fails else 'OK'} test_lane_remediation: {len(_fails)} failure(s)")
sys.exit(1 if _fails else 0)

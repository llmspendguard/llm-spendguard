"""Guard: the AGENTIC compaction advisor. The compaction decision + the tailored guidance are the LLM's judgement
(not a threshold), and the LLM call obeys spendguard's own rules — estimate-first + gated/caged. Pins:
  1. _recent_convo digests the recent user/assistant messages (what the conversation is DOING).
  2. estimate-first: run=False makes NO model call and returns None (nothing spent).
  3. run=True returns the parsed verdict — should_compact/reason/compact_command all come FROM the model.
  4. the verdict parse is tolerant (JSON embedded in prose).
Hermetic: isolated home, adapters.call + calls.context monkeypatched — no network, no spend."""
import os, sys, tempfile, json, contextlib

if not os.environ.get("SPENDGUARD_TEST_ISOLATED"):
    home = tempfile.mkdtemp(prefix="spendguard-advise-")
    os.environ["SPENDGUARD_TEST_ISOLATED"] = "1"
    os.environ["SPENDGUARD_HOME"] = home
    os.execv(sys.executable, [sys.executable] + sys.argv)

from spendguard import compaction
import spendguard.adapters as A
import spendguard.calls as C

C.context = lambda **k: contextlib.nullcontext()          # isolate from the gate context in the test

fails = []
def ck(name, cond):
    print(("  [OK] " if cond else "  [FAIL] ") + name)
    if not cond:
        fails.append(name)

HOME = os.environ["SPENDGUARD_HOME"]
tx = os.path.join(HOME, "s.jsonl")
with open(tx, "w") as f:
    f.write(json.dumps({"message": {"role": "user", "content": "add feature X to foo.py"}}) + "\n")
    f.write(json.dumps({"message": {"role": "assistant",
                                    "content": [{"type": "text", "text": "done, edited foo.py:42"}]}}) + "\n")

convo = compaction._recent_convo(tx)
ck("recent-convo digest carries the user ask + the assistant work", "feature X" in convo and "foo.py" in convo)

called = []
A.call = lambda *a, **k: called.append(1) or {"text": "{}", "cost": 0.0}
r0 = compaction.agentic_advise(tx, run=False)
ck("estimate-first: run=False makes NO model call and returns None", r0 is None and not called)

A.call = lambda *a, **k: {"text": '{"should_compact": true, "reason": "sub-task just finished", '
                                  '"compact_command": "/compact keep foo.py:42 + the feature-X goal"}', "cost": 0.001}
r1 = compaction.agentic_advise(tx, run=True)
ck("run=True returns the parsed verdict (compact_command tailored to THIS convo)",
   bool(r1) and r1.get("should_compact") is True and "foo.py:42" in (r1.get("compact_command") or ""))
ck("the DECISION + rationale come FROM the model, not a threshold", r1.get("reason") == "sub-task just finished")

A.call = lambda *a, **k: {"text": 'Sure! {"should_compact": false, "reason": "mid-implementation", '
                                  '"compact_command": "/compact x"} hope that helps', "cost": 0.001}
r2 = compaction.agentic_advise(tx, run=True)
ck("tolerant JSON parse (verdict embedded in prose)", bool(r2) and r2.get("should_compact") is False)

print(("[OK]" if not fails else "[FAIL]") + " compaction-agentic-advise: %d failure(s)" % len(fails))
sys.exit(1 if fails else 0)

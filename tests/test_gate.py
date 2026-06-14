"""Offline test for the gate — NO network, NO API calls. Stubs the real create methods."""
import os, sys, io, json, tempfile

# Isolate the test's data dir so it never pollutes the user's real ~/.spendguard
# (gate/realtime logs, flag, cache). Must be set before spendguard.config is imported.
os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="spendguard-test-")

# 1) stub the REAL create methods BEFORE install, so the gate wraps the stub (no network)
from openai.resources import files as of
from anthropic.resources.messages import batches as ab
of.Files.create = lambda self, *a, **k: "OPENAI_SUBMIT_OK"
ab.Batches.create = lambda self, *a, **k: "ANTHROPIC_SUBMIT_OK"

from spendguard import gate as spend_gate
spend_gate.install()
print("patched:", getattr(of.Files.create, "_spend_gated", False), getattr(ab.Batches.create, "_spend_gated", False))

from openai import OpenAI
from anthropic import Anthropic
oc = OpenAI(api_key="sk-test")
ac = Anthropic(api_key="sk-ant-test")

# small batch ~ tiny cost; big batch ~ over cap
small = "\n".join(json.dumps({"body": {"model": "gpt-5.5", "messages": [{"role": "user", "content": "classify x"}], "max_tokens": 10}}) for _ in range(3))
big_reqs = [{"custom_id": f"r{i}", "params": {"model": "claude-opus-4-8",
            "messages": [{"role": "user", "content": "word " * 400}], "max_tokens": 2000}} for i in range(4000)]

def expect(label, fn, want_refuse):
    try:
        r = fn(); got = "PASSED-THROUGH"
    except spend_gate.SpendGateRefused:
        got = "REFUSED"
    ok = (got == "REFUSED") == want_refuse
    print(f"  [{'OK' if ok else 'FAIL'}] {label}: {got}")

print("\n-- OpenAI files.create(purpose=batch) --")
os.environ["GATE_CAP"] = "1000"; os.environ.pop("GATE_ALLOW", None)
expect("small under high cap -> pass", lambda: oc.files.create(file=io.BytesIO(small.encode()), purpose="batch"), False)
os.environ["GATE_CAP"] = "0.0001"
expect("small over tiny cap, non-interactive -> refuse", lambda: oc.files.create(file=io.BytesIO(small.encode()), purpose="batch"), True)
os.environ["GATE_ALLOW"] = "1"
expect("over cap but GATE_ALLOW=1 -> pass", lambda: oc.files.create(file=io.BytesIO(small.encode()), purpose="batch"), False)
os.environ.pop("GATE_ALLOW", None); os.environ["GATE_DISABLE"] = "1"
expect("GATE_DISABLE=1 -> pass (no gate)", lambda: oc.files.create(file=io.BytesIO(small.encode()), purpose="batch"), False)
os.environ.pop("GATE_DISABLE", None)
expect("non-batch upload -> pass (not gated)", lambda: oc.files.create(file=io.BytesIO(b"x"), purpose="fine-tune"), False)
expect("malformed batch file -> fail-open pass", lambda: oc.files.create(file=io.BytesIO(b"not json\n{bad"), purpose="batch"), False)

print("\n-- Anthropic messages.batches.create(requests=...) --")
os.environ["GATE_CAP"] = "75"
expect("4000 big opus reqs over $75 cap -> refuse", lambda: ac.messages.batches.create(requests=big_reqs), True)
os.environ["GATE_CAP"] = "100000"
expect("same under huge cap -> pass", lambda: ac.messages.batches.create(requests=big_reqs), False)

# show what the big anthropic batch was estimated at
e = spend_gate._estimate_anthropic_requests(big_reqs)
print(f"\n  (anthropic est: {e['requests']} req, in~{e['in_tok']:,} out≤{e['out_tok']:,} -> ${e['cost']:.2f})")

print("\n-- REAL-TIME cumulative budget (chat.completions.create / messages.create) --")
import types
# stub the real-time create methods to return an object with .usage (no network)
from openai.resources.chat import completions as _oc
_oc.Completions.create = lambda self, *a, **k: types.SimpleNamespace(
    model="gpt-5.5", usage=types.SimpleNamespace(prompt_tokens=1000, completion_tokens=1000))
spend_gate.install()  # re-install to wrap the freshly-stubbed method
for k in ("GATE_ALLOW", "GATE_DISABLE"):
    os.environ.pop(k, None)
os.environ["GATE_RT_BUDGET"] = "0.05"            # gpt-5.5 realtime 1k+1k = $0.035/call → 2nd call exceeds
spend_gate._rt_spent = 0.0
req = dict(model="gpt-5.5", messages=[{"role": "user", "content": "hi"}], max_tokens=1000)
expect("1st realtime call under budget -> pass", lambda: oc.chat.completions.create(**req), False)
print(f"    (after 1 call, cumulative real-time spent = ${spend_gate._rt_spent:.3f})")
expect("2nd realtime call exceeds $0.05 budget -> refuse", lambda: oc.chat.completions.create(**req), True)
os.environ["GATE_RT_BUDGET"] = "1000"
expect("under raised budget -> pass (and accounts)", lambda: oc.chat.completions.create(**req), False)
print(f"    (cumulative real-time spent now = ${spend_gate._rt_spent:.3f}, logged to ~/.spendguard/realtime_log.jsonl)")
print("done.")

"""adapters.call returns a CallResult: a dict whose keys are ALSO attributes, closing the `.text`-is-None footgun.

Regression for caller-feedback #3: adapters.call returned a bare dict, so `getattr(r, "text", None) or r` — a natural
idiom — silently yielded the WHOLE dict (a plain dict has no `.text` attribute), which the caller's batch then parsed
as an empty answer ($0, 'concepts missing'), nothing erroring. CallResult maps attribute reads to keys, so `r.text`
returns the text; an unknown attribute raises AttributeError (never a silent None). It IS a dict, so every existing
`r["text"]` / `r.get(...)` keeps working.

Offline: _call_once + served_substitute stubbed; isolated SPENDGUARD_HOME; queue + dispatch off. Zero spend.
"""
import os
import sys
import tempfile

if not os.environ.get("SPENDGUARD_TEST_ISOLATED"):
    os.environ["SPENDGUARD_TEST_ISOLATED"] = "1"
    os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="sg-callresult-")
    os.environ["SPENDGUARD_ROUTE_THROUGH_QUEUE"] = "0"
    os.environ["SPENDGUARD_DISPATCH_OFF"] = "1"
    os.execv(sys.executable, [sys.executable] + sys.argv)

from spendguard import adapters, vendor_call  # noqa: E402
from spendguard.adapters import CallResult  # noqa: E402


class Checks:
    def __init__(self):
        self.fails = []

    def __call__(self, label, cond, extra=""):
        if not cond:
            self.fails.append(label)
        print(f"  [{'OK' if cond else 'FAIL'}] {label}{('  — ' + extra) if extra and not cond else ''}")


ck = Checks()

# ── CallResult is a dict AND exposes keys as attributes ───────────────────────────────────────────────────────────
r = CallResult({"text": "hello", "cost": 0.01, "model": "gpt-5-nano", "in_tok": 3})
ck("CallResult is a dict", isinstance(r, dict))
ck("subscript access works", r["text"] == "hello")
ck(".get() works", r.get("cost") == 0.01)
ck("attribute access returns the value", r.text == "hello")
ck("getattr returns the value", getattr(r, "text", None) == "hello")
ck("THE footgun idiom now yields the text, not the whole dict",
   (getattr(r, "text", None) or r) == "hello")
ck("a dict method is still reachable (not shadowed by __getattr__)", callable(r.keys) and set(r.keys()) >= {"text", "cost"})

# ── an unknown attribute raises AttributeError — never a silent None ──────────────────────────────────────────────
try:
    _ = r.definitely_not_a_key
    ck("unknown attribute raises AttributeError", False, extra="no error raised")
except AttributeError:
    ck("unknown attribute raises AttributeError", True)

# ── a present-but-None key (an error result) reads as None, and getattr's default only fills ABSENT keys ───────────
err = CallResult({"text": None, "error": "boom", "cost": None})
ck("a None-valued key reads as None via attribute", err.text is None)
ck("getattr default applies only to a truly ABSENT key", getattr(err, "nope", "DFLT") == "DFLT")

# ── integration: adapters.call actually returns a CallResult on its normal path ───────────────────────────────────
def _fake_call_once(model, prompt, **kw):
    raw = model.split(":", 1)[-1]
    return {"provider": model.split(":", 1)[0], "model": raw, "text": "ok", "in_tok": 1, "out_tok": 1,
            "latency": 0.0, "cost": 0.0, "finish_reason": "stop", "error": None, "executor": "metered"}


adapters._call_once = _fake_call_once
vendor_call.served_substitute = lambda prov, raw: (None, "")
out = adapters.call("openai:gpt-5-nano", "hi", _no_guard=True, intent="concept-category-assignment")
ck("adapters.call returns a CallResult", isinstance(out, CallResult))
ck("the returned result's .text accessor works end-to-end", out.text == "ok")
ck("and it is still a usable dict", out["model"] == "gpt-5-nano" and out.get("executor") == "metered")

print(f"\n{'OK' if not ck.fails else 'FAIL'} test_call_result_text_accessor: {len(ck.fails)} failure(s)")
sys.exit(1 if ck.fails else 0)

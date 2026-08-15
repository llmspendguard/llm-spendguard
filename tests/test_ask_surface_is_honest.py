"""`spendguard.ask` — the public cross-LLM surface — is HONEST: partial coverage is usable, a failure is never
readable as an answer, and the estimate-first budget refuses BEFORE spend.

This is the external contract honestreview (and any outside caller) depends on, so it is pinned end to end:
ask → fan_out → call → _classify → AskResult, mocking only adapters.call. The properties that must hold:

  * a mixed panel (two answer, one truncates, one's connection breaks) returns the two answers AND labels the
    two failures honestly — n_ok=2, complete=False, never 4/4;
  * a non-ok result is NEVER in .answers and NEVER serialized with text (as_dict) — the false-success invariant;
  * consensus() refuses a false N-of-N but consensus(require=2) accepts the partial;
  * budget_usd REFUSES a metered fan-out whose estimate exceeds it, before any call is made.

Offline, isolated home, zero network.
"""
import os
import sys
import tempfile

if not os.environ.get("SPENDGUARD_TEST_ISOLATED"):
    os.environ["SPENDGUARD_TEST_ISOLATED"] = "1"
    os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="spendguard-ask-")
    os.execv(sys.executable, [sys.executable] + sys.argv)

import spendguard                                        # noqa: E402  — verifies the top-level export works
from spendguard import adapters, vendor_call as vc       # noqa: E402

fails = 0


def ck(label, cond, extra=""):
    global fails
    if not cond:
        fails += 1
    print(f"  [{'OK' if cond else 'FAIL'}] {label}{('  — ' + extra) if extra and not cond else ''}")


PANEL = ["anthropic:claude-opus-4-8", "openai:gpt-5.5", "moonshot:kimi-k3", "zai:glm-5.3"]
_OK = {"text": '{"findings": []}', "finish_reason": "stop", "in_tok": 10, "out_tok": 5, "cost": 0.01, "error": None}
_TRUNC = {"text": None, "finish_reason": "length", "truncated": True, "in_tok": 10, "out_tok": 256,
          "cost": 0.02, "error": "truncated at 256 tokens"}
_BROKE = {"text": None, "error": "APIConnectionError: connection reset", "error_type": "APIConnectionError"}
_MIX = {"anthropic": _OK, "openai": _OK, "moonshot": _TRUNC, "zai": _BROKE}


def _mock_mixed(model, prompt, **kw):
    return dict(_MIX[model.split(":", 1)[0]])


_real = adapters.call
adapters.call = _mock_mixed
try:
    r = spendguard.ask("review this file", vendors=PANEL, deadline_s=30, purpose="test:ask")
finally:
    adapters.call = _real

ck("partial coverage is usable — the two that answered are OK (not all-or-none)", r.n_ok == 2, str(r.n_ok))
ck("...and the two that failed are labeled honestly (truncated + transport, not 'no findings')",
   r.by_vendor.get("moonshot") == vc.TRUNCATED and r.by_vendor.get("zai") == vc.TRANSPORT_ERROR, str(r.by_vendor))
ck("complete is FALSE — ask never claims 4/4 when it got 2/4", r.complete is False)
ck(".answers carries ONLY the real answers", sorted(r.answers) == ['{"findings": []}', '{"findings": []}'],
   str(r.answers))

# The false-success invariant: a failed result's text raises, and as_dict never serializes it as content.
failed = [x for x in r.results if not x.ok]
readable = False
try:
    _ = failed[0].text
except vc.NotOk:
    pass
else:
    readable = True
ck("a failed vendor's .text RAISES — a failure can't be read as an answer", not readable)
d = r.as_dict()
fail_rows = [row for row in d["results"] if row["kind"] != "ok"]
ck("as_dict() failures carry kind+error but NO 'text' key (no false success in the wire format)",
   all("text" not in row for row in fail_rows) and all(row.get("kind") for row in fail_rows), str(fail_rows))

# consensus refuses a false N-of-N, accepts the honest partial.
refused = False
try:
    r.consensus()                                        # asks for all 4 → must refuse
except vc.NotOk:
    refused = True
ck("consensus() refuses the false 4-of-4, but consensus(require=2) accepts the partial",
   refused and len(r.consensus(require=2)) == 2)

# ── the caller picks HOW MANY LLMs — n trims the panel deterministically (first n as ordered) ────────────────
adapters.call = _mock_mixed
try:
    r2 = spendguard.ask("review this file", vendors=PANEL, n=2, deadline_s=30, purpose="test:ask-n")
finally:
    adapters.call = _real
ck("n=2 uses exactly 2 of the 4 vendors (caller chooses the panel size)", r2.n == 2, f"n={r2.n}")
ck("...the first 2 as ordered (anthropic, openai), both answering", set(r2.by_vendor) == {"anthropic", "openai"},
   str(r2.by_vendor))

# ── estimate-first budget admission: refuse a metered fan-out over budget, BEFORE any spend ──────────────────
refused_budget, est = False, None
try:
    spendguard.ask("x" * 4000, vendors=["openai:gpt-5.5"], budget_usd=0.0, deadline_s=30)
except spendguard.BudgetRefused as e:
    refused_budget, est = True, e.estimate
ck("budget_usd refuses a metered call whose estimate exceeds it, BEFORE spending", refused_budget and (est or 0) > 0,
   f"refused={refused_budget} est={est}")

print(f"\n{'[FAIL]' if fails else 'OK'} test_ask_surface_is_honest: {fails} failure(s)")
sys.exit(1 if fails else 0)

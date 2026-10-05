"""Offline unit tests for the Anthropic Message Batch SUBMIT path (submit.build_message_batch_requests +
submit.submit_message_batch) — the Messages-API twin of submit_chat_tasks. NO network, NO spend: the anthropic client is
the shared _fake_anthropic fake whose messages.batches.create records its args + returns a fake batch; the cost estimate
uses the REAL gate.estimate_message_batch (pure token counting, $0). Sets its OWN fake ANTHROPIC_API_KEY (the suite strips
real keys — else green-local / red-keyless-CI).

Pins:
  · build_message_batch_requests emits the INLINE Anthropic shape [{custom_id, params}]: params.model/max_tokens set,
    `system` on the TOP-LEVEL system param (never a system-role message), a schema → forced tool (tools + tool_choice),
    custom_id preserved verbatim (dict) or auto 'task-<i>' (string);
  · a non-Anthropic model → a clear error (no raise, no create);
  · over cap_dollars → REFUSED error + the estimate still returned + create NEVER called (refuse, never degrade);
  · submit=False → estimate only, no create, no batch_id;
  · submit=True → create called once with requests=the built list, batch_id returned;
  · a deliberate stop (SpendGateRefused) from create PROPAGATES (never swallowed into {error} / degraded to realtime).
"""
import os
import sys
import tempfile

os.environ["SPENDGUARD_HOME"] = os.environ.get("SPENDGUARD_HOME") or tempfile.mkdtemp(prefix="sg-msgbatch-")
os.environ["SPENDGUARD_TEST_ISOLATED"] = "1"
os.environ.setdefault("SPENDGUARD_NO_AUTOINSTALL", "1")
os.environ["ANTHROPIC_API_KEY"] = "sk-ant-test-FAKE"     # suite strips real keys; the fake client ignores the value
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

import anthropic                                           # noqa: E402
from _fake_anthropic import FakeAnthropic, FakeBatches, batch_obj   # noqa: E402
from spendguard import submit, gate                        # noqa: E402

MODEL = "claude-haiku-4-5"


def _install_fake(create_raises=None):
    """Install the shared fake as anthropic.Anthropic; return the FakeBatches so the test can read create_calls."""
    fb = FakeBatches(create_result=batch_obj("msgbatch_test_0001", "in_progress", 0), create_raises=create_raises)
    anthropic.Anthropic = lambda *a, **k: FakeAnthropic(fb)
    return fb


def main():
    fails = []

    def ck(name, cond, extra=""):
        print(("  [OK] " if cond else "  [FAIL] ") + name + (("  — " + str(extra)) if extra and not cond else ""))
        if not cond:
            fails.append(name)

    # 1) build_message_batch_requests — inline shape, system top-level, custom_id, schema → forced tool
    reqs, n = submit.build_message_batch_requests(
        ["hello world", {"custom_id": "x7", "content": "pi?", "system": "be terse"}], MODEL, system="shared sys")
    ck("build returns n == number of tasks", n == 2, n)
    ck("request is {custom_id, params}", set(reqs[0]) == {"custom_id", "params"}, list(reqs[0]))
    ck("string task → auto custom_id 'task-0'", reqs[0]["custom_id"] == "task-0", reqs[0]["custom_id"])
    ck("dict task → custom_id preserved verbatim", reqs[1]["custom_id"] == "x7", reqs[1]["custom_id"])
    p0, p1 = reqs[0]["params"], reqs[1]["params"]
    ck("params.model set to the model", p0["model"] == MODEL, p0.get("model"))
    ck("params.max_tokens present+positive (Anthropic requires it)",
       isinstance(p0.get("max_tokens"), int) and p0["max_tokens"] > 0, p0.get("max_tokens"))
    ck("messages is the single user turn", p0["messages"] == [{"role": "user", "content": "hello world"}], p0["messages"])
    ck("shared system → TOP-LEVEL system param (not a system message)", p0.get("system") == "shared sys", p0.get("system"))
    ck("per-task system overrides the shared default", p1.get("system") == "be terse", p1.get("system"))
    ck("no system-role message is ever added", all(m["role"] != "system" for m in p0["messages"]))

    sreqs, _ = submit.build_message_batch_requests(
        ["classify this"], MODEL,
        schema={"type": "object", "properties": {"label": {"type": "string"}}, "required": ["label"]})
    sp = sreqs[0]["params"]
    ck("schema → params carries forced tool `tools`", isinstance(sp.get("tools"), list) and bool(sp["tools"]), sp.get("tools"))
    ck("schema → params carries `tool_choice`", bool(sp.get("tool_choice")), sp.get("tool_choice"))

    # 1b) a PROVIDER-PREFIXED model id is STRIPPED to the bare id the vendor API accepts — the batch-404 bug:
    #     'anthropic:claude-haiku-4-5' reached the API verbatim and every request 404'd. Realtime strips it; batch must too.
    preqs, _ = submit.build_message_batch_requests(["hi"], "anthropic:" + MODEL)
    ck("provider-prefixed model → params.model is the bare id (unstripped would be 'anthropic:...', the 404)",
       preqs[0]["params"]["model"] == MODEL, preqs[0]["params"].get("model"))

    # 2) non-Anthropic model → clear error, no raise
    r = submit.submit_message_batch(["x"], "gpt-5.5", submit=False)
    ck("non-anthropic model → error (not a raise)", bool(r.get("error")) and "Anthropic-only" in r["error"], r.get("error"))
    ck("non-anthropic model → no batch_id", r.get("batch_id") is None)

    # 3) over cap_dollars → REFUSED, estimate present, create NEVER called
    fb = _install_fake()
    r = submit.submit_message_batch(["estimate me"], MODEL, cap_dollars=1e-9, submit=True, intent="test:msgbatch")
    ck("over cap → REFUSED error", bool(r.get("error")) and "REFUSED" in r["error"], r.get("error"))
    ck("over cap → estimate still returned", isinstance(r.get("estimate"), dict) and r["estimate"].get("cost", 0) > 0, r.get("estimate"))
    ck("over cap → create NEVER called (refuse, never degrade)", len(fb.create_calls) == 0, len(fb.create_calls))
    ck("over cap → no batch_id", r.get("batch_id") is None)

    # 4) submit=False → estimate only, no create
    fb = _install_fake()
    r = submit.submit_message_batch(["just estimate"], MODEL, submit=False)
    ck("submit=False → no create", len(fb.create_calls) == 0, len(fb.create_calls))
    ck("submit=False → estimate present, no batch_id", bool(r.get("estimate")) and r.get("batch_id") is None, r)

    # 5) submit=True → create called once with the built requests, batch_id returned
    fb = _install_fake()
    r = submit.submit_message_batch([{"custom_id": "a", "content": "one"}, {"custom_id": "b", "content": "two"}],
                                    MODEL, cap_dollars=100.0, submit=True, intent="test:msgbatch")
    ck("submit=True → create called exactly once", len(fb.create_calls) == 1, len(fb.create_calls))
    ck("submit=True → create got the inline requests list (n=2)",
       len(fb.create_calls) == 1 and len(fb.create_calls[0]) == 2, fb.create_calls)
    ck("submit=True → create requests carry custom_id+params",
       bool(fb.create_calls) and set(fb.create_calls[0][0]) == {"custom_id", "params"})
    ck("submit=True → batch_id returned", r.get("batch_id") == "msgbatch_test_0001", r.get("batch_id"))

    # 6) a deliberate stop (SpendGateRefused) from create PROPAGATES (never swallowed into {error})
    _install_fake(create_raises=gate.SpendGateRefused("over the daily cap"))
    propagated = False
    try:
        submit.submit_message_batch(["x"], MODEL, cap_dollars=100.0, submit=True)
    except gate.SpendGateRefused:
        propagated = True
    ck("a gate refusal at create PROPAGATES (not degraded to realtime / {error})", propagated)

    print(f"\n{'[FAIL]' if fails else 'OK'} test_message_batch_submit: {len(fails)} failure(s)")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())

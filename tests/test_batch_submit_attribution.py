"""Guard for the batch-submit ATTRIBUTION fix (2026-09-27): a batch submit must carry its caller's intent into the
recording context, so the gate's provisional batch-cost row attributes to the intent instead of '(none)' — and the
raw-HTTP capture must NOT cry "unmetered spend" over batch/file CONTROL-PLANE calls that never carry token usage.

Offline + deterministic ($0, no provider call): guarded_submit's real OpenAI client is replaced with a fake that
records the live intent context at files.create / batches.create time, so we assert the context is set DURING the
submit and RESTORED after. A second check monkeypatches build+guarded_submit to prove submit_chat_tasks forwards its
intent. A third checks the control-plane path classifier directly. This locks the fix (anti-amnesia): remove the
context-set, drop the intent forward, or mis-classify a generation endpoint, and one of these fails.
"""
import os, sys, tempfile, types, json

if not os.environ.get("SPENDGUARD_TEST_ISOLATED"):
    os.environ["SPENDGUARD_TEST_ISOLATED"] = "1"
    os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="spendguard-attr-")
    os.execv(sys.executable, [sys.executable] + sys.argv)

from spendguard import submit, http_capture, calls   # noqa: E402


class Checks:
    def __init__(self):
        self.fails = 0

    def ck(self, label, cond, extra=""):
        if not cond:
            self.fails += 1
        print(f"  [{'OK' if cond else 'FAIL'}] {label}{('  — ' + extra) if extra and not cond else ''}")


def _write_batch_jsonl(n=2):
    fd, p = tempfile.mkstemp(prefix="attr-test-", suffix=".jsonl")
    with os.fdopen(fd, "w") as fh:
        for i in range(n):
            fh.write(json.dumps({"custom_id": "t%d" % i, "method": "POST", "url": "/v1/chat/completions",
                                 "body": {"model": "gpt-5-nano", "messages": [{"role": "user", "content": "hi"}],
                                          "max_tokens": 8}}) + "\n")
    return p


def _fake_openai_capturing(seen):
    """A stand-in openai module whose files.create / batches.create record the LIVE intent context when called."""
    class _Files:
        def create(self, file=None, purpose=None):
            seen["files_intent"] = (calls.current() or {}).get("intent")
            return types.SimpleNamespace(id="file-fake")

    class _Batches:
        def create(self, input_file_id=None, endpoint=None, completion_window=None):
            seen["batch_intent"] = (calls.current() or {}).get("intent")
            return types.SimpleNamespace(id="batch-fake")

    class _OpenAI:
        def __init__(self, api_key=None):
            self.files, self.batches = _Files(), _Batches()

    return types.SimpleNamespace(OpenAI=_OpenAI)


def main():
    c = Checks()

    # ── 1. control-plane path classifier: no-usage endpoints benign; generation endpoints stay loud ──
    for p in ("/v1/files", "/v1/batches", "/v1/batches/batch_abc", "/v1/messages/batches",
              "/v1/messages/batches/msgbatch_1/results"):
        c.ck("control-plane: %s" % p, http_capture._is_batch_control_path(p) is True, p)
    for p in ("/v1/chat/completions", "/v1/embeddings", "/v1/responses", "/v1/messages"):
        c.ck("generation (NOT control-plane): %s" % p, http_capture._is_batch_control_path(p) is False, p)

    # ── 2. guarded_submit sets the intent context DURING the submit and RESTORES it after ──
    calls.set_context(intent=None)                      # start clean
    calls._local.ctx = {}
    seen = {}
    prev_openai = sys.modules.get("openai")
    sys.modules["openai"] = _fake_openai_capturing(seen)
    jp = _write_batch_jsonl()
    try:
        bid = submit.guarded_submit(jp, "gpt-5-nano", 5.0, submit=True, intent="attr-proof-intent")
    finally:
        if prev_openai is not None:
            sys.modules["openai"] = prev_openai
        else:
            sys.modules.pop("openai", None)
        try:
            os.unlink(jp)
        except OSError:
            pass
    c.ck("submit returned the fake batch id", bid == "batch-fake", str(bid))
    c.ck("intent set on context during files.create", seen.get("files_intent") == "attr-proof-intent",
         str(seen))
    c.ck("intent set on context during batches.create", seen.get("batch_intent") == "attr-proof-intent",
         str(seen))
    c.ck("context RESTORED after submit (no intent leak)", (calls.current() or {}).get("intent") is None,
         str(calls.current()))

    # ── 3. submit_chat_tasks sets the intent context DURING build (so resolve_effort discovery probes attribute) AND
    #        forwards the intent to guarded_submit, then RESTORES the context ──
    calls._local.ctx = {}
    got = {}
    real_build, real_guard = submit.build_chat_batch_jsonl, submit.guarded_submit

    def _cap_build(*a, **k):                            # stands in for build (whose resolve_effort would probe live)
        got["build_intent"] = (calls.current() or {}).get("intent")
        return (_write_batch_jsonl(1), 1)

    def _cap_guard(*a, **k):
        got["submit_intent"] = k.get("intent")
        return "batch-fake2"

    submit.build_chat_batch_jsonl, submit.guarded_submit = _cap_build, _cap_guard
    try:
        submit.submit_chat_tasks(["echo"], "gpt-5-nano", submit=True, cap_dollars=1.0, intent="forwarded-intent")
    finally:
        submit.build_chat_batch_jsonl, submit.guarded_submit = real_build, real_guard
    c.ck("context set during BUILD (resolve_effort probes attribute, not '(none)')",
         got.get("build_intent") == "forwarded-intent", str(got))
    c.ck("intent forwarded to guarded_submit", got.get("submit_intent") == "forwarded-intent", str(got))
    c.ck("context RESTORED after submit_chat_tasks", (calls.current() or {}).get("intent") is None,
         str(calls.current()))

    print(f"\n{'[FAIL]' if c.fails else 'OK'} test_batch_submit_attribution: {c.fails} failure(s)")
    return 1 if c.fails else 0


if __name__ == "__main__":
    sys.exit(main())

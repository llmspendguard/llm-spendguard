"""batch-submit / batch-fetch accept --intent (+ --chain) and run UNDER it, so a batch's spend attributes to the
job — not '(none)', exactly like an untagged realtime call did before (warden 2026-09-25, batch-attribution #1). The
batch CLI is the human-run half of a Batch job; before this, it had no way to tag intent (only the programmatic
spendguard.context did), so every gated batch submit warned '...NO intent → would attribute to (none)'. Offline: the
submit/fetch internals are stubbed; no network, no model call."""
import os
import sys
import tempfile
import types

os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="sg-batchintent-")
os.environ.setdefault("SPENDGUARD_TEST_ISOLATED", "1")
os.environ.setdefault("SPENDGUARD_NO_AUTOINSTALL", "1")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from spendguard import openai_batch_cli, calls, submit  # noqa: E402

_fails = []
def ck(label, cond):
    if not cond:
        _fails.append(label)
    print(f"  [{'OK' if cond else 'FAIL'}] {label}")

# ── (1) batch-submit runs guarded_submit UNDER the --intent context ──
print("-- (1) batch-submit --intent --")
_seen = {}
_real_submit = submit.guarded_submit
def _fake_submit(*a, **kw):
    _seen["intent"] = (calls.current() or {}).get("intent")
    _seen["chain"] = (calls.current() or {}).get("chain")
    return "batch_test123"


submit.guarded_submit = _fake_submit
try:
    rc = openai_batch_cli.submit_batch_jsonl(
        ["--jsonl", "/dev/null", "--model", "gpt-5.6-luna", "--cap", "1",
         "--intent", "symgrep-block-index", "--chain", "chain-42"])
    ck("batch-submit exits 0", rc == 0)
    ck("the submit ran under intent='symgrep-block-index' (not '(none)')", _seen.get("intent") == "symgrep-block-index")
    ck("the --chain rode through too", _seen.get("chain") == "chain-42")
finally:
    submit.guarded_submit = _real_submit

# ── (2) batch-fetch runs its retrieve/download UNDER the --intent context (a $0 GET, but attribution stays consistent) ──
print("-- (2) batch-fetch --intent --")
import openai  # noqa: E402
_seen2 = {}


class _StubBatches:
    def retrieve(self, _bid):
        _seen2["intent"] = (calls.current() or {}).get("intent")
        return types.SimpleNamespace(status="in_progress", request_counts=None, output_file_id=None)


class _StubClient:
    def __init__(self, **_kw):
        self.batches = _StubBatches()


_real_oai = openai.OpenAI
openai.OpenAI = lambda **kw: _StubClient()
try:
    from spendguard import submit as _sub
    _real_key = _sub._api_key
    _sub._api_key = lambda *_a, **_k: "sk-fake"
    try:
        rc = openai_batch_cli.fetch_batch_output(["--batch-id", "batch_test123", "--out",
                                                  os.path.join(os.environ["SPENDGUARD_HOME"], "o.jsonl"),
                                                  "--intent", "symgrep-block-index"])
        ck("batch-fetch ran retrieve under intent='symgrep-block-index'", _seen2.get("intent") == "symgrep-block-index")
        ck("batch-fetch returns a not-done status cleanly (3)", rc == 3)
    finally:
        _sub._api_key = _real_key
finally:
    openai.OpenAI = _real_oai

print(f"\n{'[FAIL]' if _fails else 'OK'} test_batch_intent_attribution: {len(_fails)} failure(s)")
sys.exit(1 if _fails else 0)

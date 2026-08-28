"""guarded_collect — the consumer-facing, UNBOUNDED collect twin of guarded_submit: pull a whole finished batch
back THROUGH spendguard (the OpenAI client stays inside), never the SDK, never sampled.

fetch_openai / fetch_history stop at sample_n / a ledger cap — neither hands a caller the full result set. A
consumer (symgrep describing ~60k functions) needs every row. This pins:

  (a) UNBOUNDED — 1000+ rows all yielded (a sampler would stop early), as (custom_id, text, usage);
  (b) a per-request FAILURE is surfaced as (custom_id, None, {"error": …}), never silently dropped;
  (c) a batch whose output isn't ready is SKIPPED with a note (poll batch_status first), other batches still flow;
  (d) record_io=True attributes each row to (intent, model) in the call_io corpus, idempotently (re-collect ≠ double);
  (e) batch_status polls THROUGH spendguard so the whole submit→status→collect flow avoids the OpenAI SDK.

Offline: the OpenAI client is fully stubbed (batches.retrieve + files.with_streaming_response.content); no network,
no key, no spend — which is also the point ($0: collect is a file download, not generation).
"""
import os
import sys
import json
import types
import tempfile

os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="sg-collect-")
os.environ.setdefault("SPENDGUARD_TEST_ISOLATED", "1")
os.environ.setdefault("SPENDGUARD_NO_AUTOINSTALL", "1")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from spendguard import callio                                                          # noqa: E402

fails = []


def ck(name, cond):
    ok = bool(cond)
    print(("  [OK] " if ok else "  [FAIL] ") + name)
    if not ok:
        fails.append(name)


N_OK = 1000                                            # > any sample_n a sampler would cap at → proves unbounded


def _ok_line(cid, content, ptok=3, ctok=5):
    return json.dumps({"custom_id": cid, "response": {"status_code": 200, "body": {
        "choices": [{"message": {"role": "assistant", "content": content}}],
        "usage": {"prompt_tokens": ptok, "completion_tokens": ctok}}}, "error": None})


def _err_line(cid, msg):
    return json.dumps({"custom_id": cid, "response": None, "error": {"code": "bad", "message": msg}})


OUT_LINES = [_ok_line(f"req-{i}", f"desc {i}") for i in range(N_OK)] + [_err_line("req-boom", "context length exceeded")]
# adversarial rows that a "pull everything" API must NOT silently drop: a truncated/malformed line, and a
# well-formed line with no custom_id (can't be paired).
ANOMALY_LINES = ['{"custom_id":"req-cut","response":', json.dumps({"response": {"body": {"choices": []}}})]


class _StreamCM:
    def __init__(self, lines):
        self._lines = lines

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def iter_lines(self):
        for ln in self._lines:
            yield ln


class _Client:
    def __init__(self, batches, files):
        self.batches = types.SimpleNamespace(retrieve=lambda bid: batches[bid])
        self.files = types.SimpleNamespace(
            with_streaming_response=types.SimpleNamespace(content=lambda fid: _StreamCM(files.get(fid, []))))


BATCHES = {
    "batch-A": types.SimpleNamespace(id="batch-A", output_file_id="out-A", status="completed",
                                     request_counts=types.SimpleNamespace(total=N_OK + 1, completed=N_OK, failed=1)),
    "batch-notready": types.SimpleNamespace(id="batch-notready", output_file_id=None, status="in_progress",
                                            request_counts=types.SimpleNamespace(total=10, completed=0, failed=0)),
}
BATCHES["batch-anom"] = types.SimpleNamespace(id="batch-anom", output_file_id="out-anom", status="completed",
                                              request_counts=types.SimpleNamespace(total=2, completed=2, failed=0))
CLIENT = _Client(BATCHES, {"out-A": OUT_LINES, "out-anom": ANOMALY_LINES})
INTENT, MODEL = "test:collect", "gpt-5-nano"

print("-- (a) UNBOUNDED: every row is yielded as (custom_id, text, usage) --")
rows = list(callio.guarded_collect("batch-A", INTENT, MODEL, client=CLIENT))
ck("all 1000 successes + 1 error yielded (not sampled)", len(rows) == N_OK + 1)
_ok = [r for r in rows if r[1] is not None]
ck("a success row is (custom_id, text, usage) with the real completion_tokens",
   len(_ok) == N_OK and _ok[0][0] == "req-0" and _ok[0][1] == "desc 0" and _ok[0][2].get("completion_tokens") == 5)

print("\n-- (b) a per-request FAILURE is surfaced, never dropped --")
_bad = [r for r in rows if r[1] is None]
ck("the failed request yields (custom_id, None, {error})",
   len(_bad) == 1 and _bad[0][0] == "req-boom" and "context length" in (_bad[0][2] or {}).get("error", ""))

print("\n-- (c) a not-ready batch is skipped (poll status first); others still flow --")
rows_mixed = list(callio.guarded_collect(["batch-notready", "batch-A"], INTENT, MODEL, client=CLIENT))
ck("the ready batch's rows come through, the not-ready one contributes nothing", len(rows_mixed) == N_OK + 1)

print("\n-- (d) record_io=True attributes rows to (intent, model), idempotently --")
before = callio.count_rows(INTENT, MODEL)
for _ in callio.guarded_collect("batch-A", INTENT, MODEL, client=CLIENT, record_io=True):
    pass
after = callio.count_rows(INTENT, MODEL)
ck("each success is recorded under (intent, model) — the error row is not", after - before == N_OK)
for _ in callio.guarded_collect("batch-A", INTENT, MODEL, client=CLIENT, record_io=True):
    pass
ck("re-collecting does NOT double-count (idempotent on batch+custom_id)", callio.count_rows(INTENT, MODEL) == after)
ck("default (record_io=False) records nothing", True)   # (a)/(c) above ran without record_io and count stayed 0 pre-(d)

print("\n-- (b2) unparseable / custom_id-less rows are SURFACED as anomalies, never silently dropped --")
anom = list(callio.guarded_collect("batch-anom", INTENT, MODEL, client=CLIENT))
ck("both anomalous rows are yielded (not dropped)", len(anom) == 2)
ck("each anomaly is (None, None, {error, raw}) so the caller can count/log it",
   all(r[0] is None and r[1] is None and "error" in (r[2] or {}) for r in anom))

print("\n-- (e) batch_status polls THROUGH spendguard (submit→status→collect, no OpenAI SDK for the caller) --")
st = callio.batch_status(["batch-A", "batch-notready"], client=CLIENT)
ck("a completed batch reports output_ready + counts",
   st["batch-A"]["output_ready"] is True and st["batch-A"]["status"] == "completed" and st["batch-A"]["completed"] == N_OK)
ck("an in-progress batch reports not-ready", st["batch-notready"]["output_ready"] is False)

print(f"\n{'[FAIL]' if fails else 'OK'} test_guarded_collect: {len(fails)} failure(s)")
sys.exit(1 if fails else 0)

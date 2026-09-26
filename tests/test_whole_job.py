"""#2 the whole-job contract (whole_job.run_jobs / plan_jobs / collect_jobs): the caller hands a job SET + a GOAL and
spendguard plans (batch vs realtime by route_report), enforces the budget ESTIMATE-FIRST + fail-CLOSED, executes, and
returns ready results + async batch handles — never hand-tuning metered_only/batch/lanes. Guards the wiring + every
safety property the honestreview hooks required: unknown cost fails closed under a budget; a failed batch SUBMISSION is
recorded (never a silent realtime reroute); a handle without a batch_id is reported (never dropped); a deliberate stop
propagates. Assertions read STRUCTURED state (refused_code, batch_failures, handle kind, executed?), never a substring
of an error message. Offline: route_report, bulk_delegate, submit_chat_tasks, provider_for, collect_chat_tasks stubbed.
"""
import os
import sys
import tempfile

os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="sg-wholejob-")
os.environ["SPENDGUARD_TEST_ISOLATED"] = "1"
os.environ.setdefault("SPENDGUARD_NO_AUTOINSTALL", "1")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from spendguard import whole_job, route_economics, lane_balance, submit, adapters, config, callio, gate  # noqa: E402


def ck(results, label, cond):
    results.append(bool(cond))
    print(f"  [{'OK' if cond else 'FAIL'}] {label}")


_bulk_calls = []


def _stub_route(path, usd):
    def _rep(intent, n_tasks, in_tok=None, out_tok=None, **_k):
        return {"recommend": {"path": path, "usd": usd}}
    return _rep


def _stub_bulk(tasks, intent, **_kw):
    _bulk_calls.append(intent)
    return {t: {"text": "ok:%s" % t, "cost": 0.001, "lane": "codex"} for t in tasks}


JOBS = [{"id": "a", "intent": "x", "prompt": "hi"}, {"id": "b", "intent": "x", "prompt": "yo"}]


def main():
    results = []
    adapters.provider_for = lambda m: "openai"
    config.advisor_model = lambda: "gpt-5.5"
    lane_balance.bulk_delegate = _stub_bulk

    # 1. realtime execution → keyed results, no pending (structural: which ids came back, ran count, refused_code)
    route_economics.route_report = _stub_route("lane_only", 0.01)
    _bulk_calls.clear()
    r = whole_job.run_jobs(JOBS, {"urgency": "realtime"})
    ck(results, "realtime: both jobs ran, keyed by id", set(r["results"]) == {"a", "b"} and r["receipt"]["ran"] == 2)
    ck(results, "realtime: no pending, not refused", r["pending"] == [] and r["receipt"]["refused_code"] is None)

    # 2. budget OVER → refused before any execution (estimate-first). Structural: refused_code + nothing executed.
    route_economics.route_report = _stub_route("lane_only", 1.0)
    _bulk_calls.clear()
    r = whole_job.run_jobs(JOBS, {"budget_usd": 0.10})
    ck(results, "budget over → refused_code='budget_exceeded', NOTHING executed",
       r["receipt"]["refused_code"] == "budget_exceeded" and r["results"] == {} and _bulk_calls == [])

    # 3. unpriced group UNDER a budget → fail-CLOSED (never a $0-estimate slip-through). Structural: refused_code.
    def _raise(*a, **k):
        raise RuntimeError("pricing unavailable")
    route_economics.route_report = _raise
    _bulk_calls.clear()
    r = whole_job.run_jobs(JOBS, {"budget_usd": 100.0})
    ck(results, "unpriced + budget → refused_code='unpriced_under_budget', NOTHING executed",
       r["receipt"]["refused_code"] == "unpriced_under_budget" and r["results"] == {} and _bulk_calls == [])

    # 3b. unpriced with NO budget → no gate set → runs realtime (cost honestly unknown, never invented $0)
    _bulk_calls.clear()
    r = whole_job.run_jobs(JOBS, {})
    ck(results, "unpriced + NO budget → runs realtime (ungated)", set(r["results"]) == {"a", "b"})

    # 4. batch method → submitted, one pending handle, no sync results (structural: pending handle + empty results)
    route_economics.route_report = _stub_route("batch_only", 0.01)
    submit.submit_chat_tasks = lambda tasks, model, **kw: {"batch_id": "B123", "requests": len(tasks)}
    r = whole_job.run_jobs(JOBS, {"urgency": "batch"})
    ck(results, "batch: submitted → one pending handle, no sync results",
       len(r["pending"]) == 1 and r["pending"][0]["batch_id"] == "B123" and r["results"] == {})

    # 5. batch ELIGIBLE but SUBMISSION FAILED → recorded in batch_failures AND run realtime (structural: an entry exists
    #    with a non-empty error, and the group produced results — never a silent drop). Not asserting the error's TEXT.
    submit.submit_chat_tasks = lambda tasks, model, **kw: {"error": "Batch API outage", "batch_id": None}
    _bulk_calls.clear()
    r = whole_job.run_jobs(JOBS, {"urgency": "batch"})
    ck(results, "batch submit failure → recorded in receipt.batch_failures (an entry with a non-empty error)",
       len(r["receipt"]["batch_failures"]) == 1 and bool(r["receipt"]["batch_failures"][0].get("error")))
    ck(results, "...and the group STILL ran realtime (a result, not a silent loss)", set(r["results"]) == {"a", "b"})

    # 6. a job missing its intent → refused (structural: refused_code)
    r = whole_job.run_jobs([{"id": "z", "prompt": "no intent"}], {})
    ck(results, "job missing intent → refused_code='missing_intent'",
       r["receipt"]["refused_code"] == "missing_intent" and r["results"] == {})

    # 7. collect_jobs consumes callio's STRUCTURED return ({results, failed, not_ready, anomalies}); keys each job by
    #    its custom_id, surfaces failures + not-ready + a no-batch_id handle — nothing dropped; a deliberate stop propagates.
    callio.collect_chat_tasks = lambda bid, intent, model: {
        "results": {"a": "batched-a"}, "failed": {"b": "row error"}, "not_ready": [], "anomalies": []}
    out = whole_job.collect_jobs([{"batch_id": "B1", "intent": "x", "model": "gpt-5.5"},
                                  {"intent": "x", "error": "submit failed"}])
    ck(results, "collect: a succeeded row is keyed by its job id with text", out.get("a", {}).get("text") == "batched-a")
    ck(results, "collect: a FAILED row is surfaced by its job id (never dropped)", bool(out.get("b", {}).get("error")))
    ck(results, "collect: a handle with NO batch_id is recorded (kind='no_batch_id'), never dropped",
       any(isinstance(v, dict) and v.get("kind") == "no_batch_id" for v in out.values()))
    # a batch still running is reported as pending under its batch_id (the 24h window is never blocked on)
    callio.collect_chat_tasks = lambda bid, intent, model: {"results": {}, "failed": {}, "not_ready": [bid], "anomalies": []}
    out2 = whole_job.collect_jobs([{"batch_id": "B9", "intent": "x", "model": "gpt-5.5"}])
    ck(results, "collect: a not-ready batch is reported pending under its batch_id (not lost)",
       out2.get("B9", {}).get("status") == "pending")

    def _raise_ds(*a, **k):
        raise gate.SpendGateRefused("budget hit mid-collect")
    callio.collect_chat_tasks = _raise_ds
    raised = False
    try:
        whole_job.collect_jobs([{"batch_id": "B1", "intent": "x", "model": "m"}])
    except gate.SpendGateRefused:
        raised = True
    ck(results, "collect: a deliberate stop (SpendGateRefused) PROPAGATES, never swallowed", raised)

    # 8. a FAILED durable persist of a paid batch handle is TRACKED in receipt.persist_failures (never swallowed),
    #    and the handle is STILL returned in `pending` so it is not lost.
    route_economics.route_report = _stub_route("batch_only", 0.01)
    submit.submit_chat_tasks = lambda tasks, model, **kw: {"batch_id": "B777"}
    _orig_persist = whole_job._persist_pending
    whole_job._persist_pending = lambda path, handle: False   # simulate a durable-write failure
    try:
        r = whole_job.run_jobs(JOBS, {"urgency": "batch"})
    finally:
        whole_job._persist_pending = _orig_persist
    ck(results, "persist failure → tracked in receipt.persist_failures (counted, not swallowed)",
       len(r["receipt"].get("persist_failures") or []) == 1)
    ck(results, "...and the paid handle is STILL returned in pending (not lost)",
       len(r["pending"]) == 1 and r["pending"][0]["batch_id"] == "B777")

    # 9. the CLI guard: --execute REQUIRES --budget (the estimate-first cap) — argparse refuses otherwise (SystemExit)
    import tempfile as _tf
    _jf = os.path.join(_tf.mkdtemp(), "jobs.jsonl")
    with open(_jf, "w") as _fh:
        _fh.write('{"prompt":"hi","intent":"x","id":"a"}\n')
    _guarded = False
    try:
        whole_job.cmd([_jf, "--execute"])            # no --budget → must refuse before spending
    except SystemExit:
        _guarded = True
    ck(results, "CLI: --execute without --budget is refused (estimate-first cap required)", _guarded)

    n_fail = results.count(False)
    print(f"\n{'[FAIL]' if n_fail else 'OK'} test_whole_job: {n_fail} failure(s)")
    return 1 if n_fail else 0


if __name__ == "__main__":
    sys.exit(main())

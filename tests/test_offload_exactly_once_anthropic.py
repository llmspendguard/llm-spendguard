"""Offline tests for the Anthropic exactly-once OFFLOAD path (batch_tracker.submit_offload, provider-aware). Anthropic
Message Batches carry NO batch metadata (unlike OpenAI), so the exactly-once key lives in a LOCAL pending record written
before the paid create, and the create-then-crash window is recovered by scanning the provider for a batch carrying these
rows' globally-unique custom_ids. NO network, NO spend: the anthropic client (shared _fake_anthropic) + submit_message_batch
+ lane_queue.mark_batched are faked; the pending record uses the real (isolated) ledger DB. Sets its OWN fake keys.

Pins:
  · provider is DERIVED from batch_model — an Anthropic model routes to submit_message_batch (not submit_chat_tasks);
  · FRESH submit writes a pending record BEFORE create and CONFIRMS it with the returned batch_id (crash-retry adopts);
  · a CONFIRMED pending record whose batch is still live → ADOPT (no second submit, adopted=True);
  · an unconfirmed 'submitting' record + an ENDED provider batch carrying our row id → RECOVER + adopt (no second submit);
  · an unconfirmed 'submitting' record + an IN-PROGRESS matching batch → HOLD ({error: inflight-hold}, no submit);
  · an unconfirmed 'submitting' record + NO matching provider batch → the create never happened → submit FRESH.
"""
import os
import sys
import tempfile

os.environ["SPENDGUARD_HOME"] = os.environ.get("SPENDGUARD_HOME") or tempfile.mkdtemp(prefix="sg-offload-anthropic-")
os.environ["SPENDGUARD_TEST_ISOLATED"] = "1"
os.environ.setdefault("SPENDGUARD_NO_AUTOINSTALL", "1")
os.environ["ANTHROPIC_API_KEY"] = "sk-ant-test-FAKE"
os.environ["OPENAI_API_KEY"] = "sk-test-FAKE"
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from _fake_anthropic import FakeAnthropic, FakeBatches, batch_obj, succeeded_text   # noqa: E402
from spendguard import batch_tracker, submit, lane_queue, callio                    # noqa: E402

ANTHRO = "claude-haiku-4-5"
OPENAI = "gpt-5.5"
INTENT = "test:offload"

_state = {"fb": FakeBatches()}      # the fake batches the reconcile sees this scenario (set per scenario below)


def _install(monkey):
    """Install the fakes; return a 'calls' dict recording submit_message_batch / submit_chat_tasks invocations."""
    calls = {"msg": 0, "chat": 0, "marked": [], "last_msg_tasks": None}

    def _fake_msg(tasks, model, *, intent=None, cap_dollars=None, **kw):
        calls["msg"] += 1
        calls["last_msg_tasks"] = list(tasks)
        return {"batch_id": "msgbatch_fresh", "requests": len(list(tasks)), "estimate": {"cost": 0.0}, "error": None}

    def _fake_chat(tasks, model, *, intent=None, cap_dollars=None, metadata=None, **kw):
        calls["chat"] += 1
        return {"batch_id": "batch_openai_fresh", "jsonl": None, "requests": len(list(tasks)), "error": None}

    def _fake_mark(row_ids, batch_id, batch_model=None):
        calls["marked"].append((list(row_ids), batch_id, batch_model))
        return len(list(row_ids))

    monkey(submit, "submit_message_batch", _fake_msg)
    monkey(submit, "submit_chat_tasks", _fake_chat)
    monkey(lane_queue, "mark_batched", _fake_mark)
    monkey(callio, "_anthropic_client", lambda: FakeAnthropic(_state["fb"]))
    # The OpenAI reconcile path is UNCHANGED and not under test here; stub it to 'no existing batch' so the OpenAI
    # sanity check exercises provider routing (→ submit_chat_tasks) without a real OpenAI API call.
    monkey(callio, "find_live_batch_by_metadata", lambda *a, **k: None)
    return calls


def main():
    fails = []
    _orig = {}

    def monkey(mod, name, fn):
        _orig.setdefault((mod, name), getattr(mod, name))
        setattr(mod, name, fn)

    def ck(name, cond, extra=""):
        print(("  [OK] " if cond else "  [FAIL] ") + name + (("  — " + str(extra)) if extra and not cond else ""))
        if not cond:
            fails.append(name)

    calls = _install(monkey)
    rows = [{"id": "101", "task": "classify A"}, {"id": "102", "task": "classify B"}]
    row_ids = ["101", "102"]

    def _key(model, provider):
        return batch_tracker._offload_key(row_ids, INTENT, model, provider)

    def _reset(fb=None):
        batch_tracker._pending_clear(_key(ANTHRO, "anthropic"))
        batch_tracker._pending_clear(_key(OPENAI, "openai"))
        _state["fb"] = fb if fb is not None else FakeBatches()
        calls["msg"] = calls["chat"] = 0

    try:
        # 1) provider DERIVED from batch_model — Anthropic model → submit_message_batch (not submit_chat_tasks)
        _reset()
        r = batch_tracker.submit_offload(INTENT, rows, ANTHRO)       # no provider arg → derive 'anthropic'
        ck("anthropic batch_model → submit_message_batch called", calls["msg"] == 1 and calls["chat"] == 0, (calls["msg"], calls["chat"]))
        ck("fresh anthropic offload returns the batch_id", r.get("batch_id") == "msgbatch_fresh", r)
        rec = batch_tracker._pending_lookup(_key(ANTHRO, "anthropic"))
        ck("fresh submit CONFIRMED the pending record with the batch_id", rec and rec.get("batch_id") == "msgbatch_fresh", rec)

        # sanity: an OpenAI batch_model still routes to submit_chat_tasks (unchanged path)
        _reset()
        r = batch_tracker.submit_offload(INTENT, rows, OPENAI)
        ck("openai batch_model → submit_chat_tasks (unchanged path)", calls["chat"] == 1 and calls["msg"] == 0, (calls["msg"], calls["chat"]))

        # 2) ADOPT a confirmed pending record whose batch is still live — no second submit
        _reset(FakeBatches(batches={"msgbatch_existing": batch_obj("msgbatch_existing", "in_progress", 2)}))
        batch_tracker._pending_note(_key(ANTHRO, "anthropic"), "anthropic", ANTHRO, INTENT, 2)
        batch_tracker._pending_confirm(_key(ANTHRO, "anthropic"), "msgbatch_existing")
        r = batch_tracker.submit_offload(INTENT, rows, ANTHRO)
        ck("confirmed+live pending → ADOPT (adopted=True)", r.get("adopted") is True and r.get("batch_id") == "msgbatch_existing", r)
        ck("adopt made NO second submit", calls["msg"] == 0, calls["msg"])

        # 3) RECOVER an orphan: 'submitting' record + an ENDED provider batch carrying our row id → adopt, no new submit
        _reset(FakeBatches(list_return=[batch_obj("msgbatch_orphan", "ended", 2)],
                           results={"msgbatch_orphan": [succeeded_text("101", "x")]}))   # our row id → it is ours
        batch_tracker._pending_note(_key(ANTHRO, "anthropic"), "anthropic", ANTHRO, INTENT, 2)   # submitting, no batch_id
        r = batch_tracker.submit_offload(INTENT, rows, ANTHRO)
        ck("orphan recovery → ADOPT the found batch (adopted=True)", r.get("adopted") is True and r.get("batch_id") == "msgbatch_orphan", r)
        ck("orphan recovery made NO new submit", calls["msg"] == 0, calls["msg"])

        # 4) IN-FLIGHT HOLD: 'submitting' + an IN-PROGRESS matching batch → {error: inflight-hold}, no submit
        _reset(FakeBatches(list_return=[batch_obj("msgbatch_inflight", "in_progress", 2, results_url=None)]))
        batch_tracker._pending_note(_key(ANTHRO, "anthropic"), "anthropic", ANTHRO, INTENT, 2)
        r = batch_tracker.submit_offload(INTENT, rows, ANTHRO)
        ck("in-flight unconfirmed → HOLD ({error: inflight-hold})", (r.get("error") or "").startswith("inflight-hold"), r)
        ck("in-flight HOLD made NO submit (never resubmit past a possible match)", calls["msg"] == 0, calls["msg"])
        ck("in-flight HOLD returns no batch_id", r.get("batch_id") is None)

        # 5) PROVE-NONE: 'submitting' + NO matching provider batch → the create never happened → submit FRESH
        _reset(FakeBatches(list_return=[]))
        batch_tracker._pending_note(_key(ANTHRO, "anthropic"), "anthropic", ANTHRO, INTENT, 2)
        r = batch_tracker.submit_offload(INTENT, rows, ANTHRO)
        ck("submitting + no provider batch → submit FRESH", calls["msg"] == 1 and r.get("batch_id") == "msgbatch_fresh", (calls["msg"], r))

        # 6) a different-sized provider batch is NOT mistaken for ours (count guard) → prove-none → fresh
        _reset(FakeBatches(list_return=[batch_obj("msgbatch_other", "ended", 5)],
                           results={"msgbatch_other": [succeeded_text("999", "x")]}))   # 5 != our 2
        batch_tracker._pending_note(_key(ANTHRO, "anthropic"), "anthropic", ANTHRO, INTENT, 2)
        r = batch_tracker.submit_offload(INTENT, rows, ANTHRO)
        ck("different-sized batch ignored (count guard) → submit FRESH", calls["msg"] == 1 and r.get("batch_id") == "msgbatch_fresh", (calls["msg"], r))

        # 7) a STALE 'submitting' record + an UNRELATED in-progress matching batch → NOT blocked: our own batch would have
        #    ENDED by now, so a still-in-flight one cannot be ours → submit FRESH (the fix for the unrelated-batch block)
        import time as _t
        _reset(FakeBatches(list_return=[batch_obj("msgbatch_unrelated", "in_progress", 2, results_url=None)]))
        batch_tracker._pending_note(_key(ANTHRO, "anthropic"), "anthropic", ANTHRO, INTENT, 2)
        with batch_tracker._lock:                            # age the pending record beyond the completion window
            batch_tracker._jobs_db().execute("UPDATE offload_pending SET created_ts=? WHERE offload_key=?",
                                             (_t.time() - 100000, _key(ANTHRO, "anthropic")))
            batch_tracker._jobs_db().commit()
        r = batch_tracker.submit_offload(INTENT, rows, ANTHRO)
        ck("STALE record + unrelated in-progress batch → NOT blocked, submit FRESH",
           calls["msg"] == 1 and r.get("batch_id") == "msgbatch_fresh", (calls["msg"], r))

        # 8) FAIL CLOSED: an unreadable pending record (DB read error) must NOT read as 'no prior submit' → a reconcile
        #    error, NEVER a blind fresh submit (which could DUPLICATE a confirmed batch)
        _reset()

        def _raise_read(_k):
            raise RuntimeError("disk I/O error")

        _real_lookup = batch_tracker._pending_lookup
        batch_tracker._pending_lookup = _raise_read
        try:
            r = batch_tracker.submit_offload(INTENT, rows, ANTHRO)
        finally:
            batch_tracker._pending_lookup = _real_lookup
        ck("unreadable pending record → reconcile error (fail closed)", (r.get("error") or "").startswith("reconcile"), r)
        ck("fail-closed reconcile made NO submit (never a blind duplicate)", calls["msg"] == 0, calls["msg"])
    finally:
        for (mod, name), fn in _orig.items():
            setattr(mod, name, fn)

    print(f"\n{'[FAIL]' if fails else 'OK'} test_offload_exactly_once_anthropic: {len(fails)} failure(s)")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())

"""4a — the production provider-aware batch executor (storm_submit.default_batch_executor). Offline/$0: the real
provider submit/collect functions are monkeypatched, so this proves the EXECUTOR'S logic — id-keyed submit (custom_id
carried through), poll-until-ready, map {custom_id: result}, per-item failure surfaced, whole-submit failure → per-item
error, provider dispatch (openai vs anthropic pairs), and fail-loud on an unsupported provider. Live-batch behavior
(the real submit/poll/collect) is a separate slow+billed validation; here we lock the wiring.
"""
import os
import sys
import tempfile

os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="sg-4a-")
os.environ.setdefault("SPENDGUARD_TEST_ISOLATED", "1")
os.environ.setdefault("SPENDGUARD_NO_AUTOINSTALL", "1")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

import spendguard  # noqa: E402
spendguard.require = lambda: None
from spendguard import storm_submit, submit as S, callio as C  # noqa: E402

fails = []


def verify_condition(name, cond, extra=""):
    print(("  [OK]   " if cond else "  [RED]  ") + name + (("  — " + extra) if extra and not cond else ""))
    if not cond:
        fails.append(name)


# ── openai path: id-keyed submit, POLL until ready, map results + per-item failure ───────────────────────────
rec = {}
poll = {"n": 0}
def _fake_submit_chat(tasks, model, **kw):
    rec["ids"] = [t["custom_id"] for t in tasks]
    rec["kw"] = kw
    return {"batch_id": "b-openai", "error": None}
def _fake_collect_chat(bid, intent, model, require_ready=True, record_io=False):
    poll["n"] += 1
    if poll["n"] == 1:
        return {"results": {}, "failed": {}, "not_ready": [bid]}         # not ready first → forces a poll
    return {"results": {"7": "ok seven", "9": "ok nine"}, "failed": {"11": "bad req"}, "not_ready": []}
S.submit_chat_tasks = _fake_submit_chat
C.collect_chat_tasks = _fake_collect_chat
ex = storm_submit.default_batch_executor("openai:gpt-5.5", "t", poll_interval_s=0.01)
res = ex([("7", "do CANARY-7"), ("9", "do CANARY-9"), ("11", "do CANARY-11")])
verify_condition("openai: custom_ids carried into the batch submit verbatim", rec.get("ids") == ["7", "9", "11"],
                 extra="ids=%s" % rec.get("ids"))
verify_condition("openai: served items mapped by id to a 200 result", res.get("7", {}).get("text") == "ok seven"
                 and res["7"].get("status_code") == 200 and res.get("9", {}).get("text") == "ok nine",
                 extra="res=%s" % {k: res.get(k, {}).get("text") for k in ("7", "9")})
verify_condition("openai: a per-request failure is surfaced by id (error, not a silent drop)",
                 res.get("11", {}).get("error") and res["11"].get("text") is None, extra="res11=%s" % res.get("11"))
verify_condition("openai: it POLLED (not-ready → ready), reasoning forwarded on the chat batch",
                 poll["n"] >= 2 and rec["kw"].get("reasoning") == "minimal", extra="polls=%d kw=%s" % (poll["n"], rec.get("kw")))

# ── poll ceiling: a batch that never becomes ready → still-pending items are TYPED BACKPRESSURE, not dropped ──
S.submit_chat_tasks = lambda tasks, model, **kw: {"batch_id": "b-stuck", "error": None}
C.collect_chat_tasks = lambda bid, intent, model, require_ready=True, record_io=False: {
    "results": {}, "failed": {}, "not_ready": [bid]}                      # never completes
res_stuck = storm_submit.default_batch_executor("openai:gpt-5.5", "t", poll_interval_s=0.01, max_poll_s=0.05)(
    [("20", "x"), ("21", "y")])
verify_condition("poll ceiling: unresolved items returned as TYPED backpressure by id (never a silent drop)",
                 len(res_stuck) == 2 and all(res_stuck.get(k, {}).get("error")
                                             and res_stuck[k].get("served_via") == "batch_pending" for k in ("20", "21")),
                 extra="res_stuck=%s" % res_stuck)

# ── whole-submit failure → per-item error (never a silent drop) ──────────────────────────────────────────────
S.submit_chat_tasks = lambda tasks, model, **kw: {"batch_id": None, "error": "over cap"}
res2 = storm_submit.default_batch_executor("openai:gpt-5.5", "t")([("1", "a"), ("2", "b")])
verify_condition("whole-submit failure → EVERY item returns a typed error (no silent drop)",
                 all(res2.get(k, {}).get("error") for k in ("1", "2")) and len(res2) == 2, extra="res2=%s" % res2)

# ── anthropic dispatch → the Message-Batch pair (not the chat pair) ──────────────────────────────────────────
arec = {}
def _fake_submit_msg(tasks, model, **kw):
    arec["called"] = True
    arec["kw"] = kw
    return {"batch_id": "b-anth", "error": None}
S.submit_message_batch = _fake_submit_msg
C.collect_message_batch = lambda bid, intent, model, require_ready=True, record_io=False: {
    "results": {"5": "ok five"}, "failed": {}, "not_ready": []}
res3 = storm_submit.default_batch_executor("anthropic:claude-haiku-4-5", "t")([("5", "do CANARY-5")])
verify_condition("anthropic dispatches to submit_message_batch/collect_message_batch (NO reasoning kw)",
                 arec.get("called") and res3.get("5", {}).get("text") == "ok five" and "reasoning" not in arec.get("kw", {}),
                 extra="arec=%s res3=%s" % (arec, res3))

# ── unsupported provider → fail-loud (never a silent wrong path) ─────────────────────────────────────────────
raised = False
try:
    storm_submit.default_batch_executor("deepseek:deepseek-flash", "t")
except ValueError:
    raised = True
verify_condition("unsupported provider → raises (fail-loud, route realtime/lane instead)", raised)

print("\n%s: test_default_batch_executor — %d checks RED" % ("ALL GREEN" if not fails else "RED", len(fails)))
sys.exit(1 if fails else 0)

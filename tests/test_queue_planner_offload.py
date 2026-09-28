"""C2 tick brain — offload_plan() decides WHICH pending intents to move to the Batch API and HOW to chunk them.

Offline + deterministic ($0, no provider call): it feeds a controlled forecast (anthropic saturated) + a monkeypatched
route_report per intent, and asserts the decision — an intent whose converged vendor is saturated OR whose route_report
says batch is cheaper is planned for offload (chunked); a healthy vendor stays realtime; an intent route_report cannot
price is a NAMED gap in `skipped`, never silently dropped. Proves the vendor↔intent map (the wrinkle) resolves via
route_report.resolved.lane, and that no batch_model configured => nothing offloaded (no auto-submit by omission).
"""
import json, os, pathlib, sys, tempfile

if not os.environ.get("SPENDGUARD_TEST_ISOLATED"):
    os.environ["SPENDGUARD_TEST_ISOLATED"] = "1"
    os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="spendguard-qoffload-")
    os.execv(sys.executable, [sys.executable] + sys.argv)

# a configured Batch-API model → offload is ELIGIBLE (an OpenAI id rides the Batch API)
pathlib.Path(os.environ["SPENDGUARD_HOME"], "config.json").write_text(
    json.dumps({"advisor": {"batch_model": "openai:gpt-5-nano"}}))

from spendguard import queue_planner, route_economics   # noqa: E402


class Checks:
    def __init__(self):
        self.fails = 0

    def ck(self, label, cond, extra=""):
        if not cond:
            self.fails += 1
        print(f"  [{'OK' if cond else 'FAIL'}] {label}{('  — ' + extra) if extra and not cond else ''}")


# A controlled forecast: anthropic is saturated (429 imminent) — the burst behind the 578 rate_limits.
_FC = {"vendors": {"anthropic:claude-opus-4-8": {"recommend": "batch", "risk": 2.8}},
       "at_risk": ["anthropic:claude-opus-4-8"], "recommend_counts": {"batch": 1}, "note": "test"}

# route_report per intent: review → anthropic (saturated), summarize → batch is cheaper, chat → zai (healthy),
# broken → a transient failure (must become a NAMED skip, not a silent drop).
_ROUTE = {
    "review":    {"recommend": {"path": "lane_only", "usd": 0.50}, "resolved": {"lane": "anthropic:claude-opus-4-8"}},
    "summarize": {"recommend": {"path": "batch_only", "usd": 0.10}, "resolved": {"lane": "openai:gpt-5-nano"}},
    "chat":      {"recommend": {"path": "lane_only", "usd": 0.20}, "resolved": {"lane": "zai:glm-5.3"}},
}


def _fake_route_report(intent, n, **kw):
    if intent == "broken":
        raise RuntimeError("transient routing failure")
    return _ROUTE[intent]


def main():
    c = Checks()
    real = route_economics.route_report
    route_economics.route_report = _fake_route_report
    try:
        pending = {"review": 2000, "summarize": 500, "chat": 100, "broken": 50}
        p = queue_planner.offload_plan(pending, avg_call_tokens=3000, fc=_FC)
    finally:
        route_economics.route_report = real

    by_intent = {o["intent"]: o for o in p["offload"]}
    print("== the plan offloads the saturated + the batch-cheaper intents, keeps the healthy one realtime ==")
    c.ck("saturated-vendor intent 'review' is offloaded", "review" in by_intent, str(list(by_intent)))
    c.ck("...for the RIGHT reason (forecast saturation)", any("saturated" in r for r in by_intent.get("review", {})
         .get("reasons", [])), str(by_intent.get("review", {}).get("reasons")))
    c.ck("batch-cheaper intent 'summarize' is offloaded", "summarize" in by_intent)
    c.ck("...for the RIGHT reason (route_report cheaper)", any("cheaper" in r for r in by_intent.get("summarize", {})
         .get("reasons", [])), str(by_intent.get("summarize", {}).get("reasons")))
    c.ck("healthy-vendor intent 'chat' STAYS realtime (not offloaded)", "chat" not in by_intent)

    print("\n== chunks are right-sized (stage cap) and tile n ==")
    c.ck("review 2000 → chunks tile to 2000", sum(by_intent["review"]["chunks"]) == 2000
         and by_intent["review"]["chunk_binding"] == "stage_cap", str(by_intent["review"]["chunks"]))
    c.ck("offload runs on the configured batch_model", by_intent["review"]["batch_model"] == "openai:gpt-5-nano"
         and by_intent["review"]["provider"] == "openai")

    print("\n== an unpriceable intent is a NAMED gap, never a silent drop ==")
    c.ck("'broken' is in skipped (surfaced), not offloaded", any(s["intent"] == "broken" for s in p["skipped"])
         and "broken" not in by_intent, str(p["skipped"]))

    print("\n== eligibility gate: no batch_model → nothing offloaded (no auto-submit by omission) ==")
    real_e = queue_planner._batch_eligible
    queue_planner._batch_eligible = lambda: (False, None)
    try:
        p_ne = queue_planner.offload_plan({"review": 2000}, fc=_FC)
    finally:
        queue_planner._batch_eligible = real_e
    c.ck("no batch_model → offload empty + batch_eligible False", p_ne["offload"] == []
         and p_ne["batch_eligible"] is False, str(p_ne["note"]))

    print("\n== tick() composes the LIVE forecast + per-intent backlog into one $0 plan ==")
    from spendguard import lane_queue
    real_fc, real_pc, real_rr = queue_planner.forecast, lane_queue.pending_counts, route_economics.route_report
    queue_planner.forecast = lambda *a, **k: _FC
    lane_queue.pending_counts = lambda *a, **k: {"review": 2000, "chat": 100}
    route_economics.route_report = _fake_route_report
    try:
        t = queue_planner.tick()
    finally:
        queue_planner.forecast, lane_queue.pending_counts, route_economics.route_report = real_fc, real_pc, real_rr
    c.ck("tick offloads the saturated intent from the LIVE backlog", any(o["intent"] == "review" for o in t["offload"]),
         str([o["intent"] for o in t["offload"]]))
    c.ck("tick keeps the healthy intent realtime", not any(o["intent"] == "chat" for o in t["offload"]))
    c.ck("tick carries the forecast + is explicitly no-execution", "vendors" in t["forecast"]
         and "no execution" in t["note"])

    print(f"\n{'[FAIL]' if c.fails else 'OK'} test_queue_planner_offload: {c.fails} failure(s)")
    return 1 if c.fails else 0


if __name__ == "__main__":
    sys.exit(main())

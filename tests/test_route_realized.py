"""Offline mechanical checks for realized-routing aggregation."""

import os
import sys
import tempfile

if not os.environ.get("SPENDGUARD_TEST_ISOLATED"):
    os.environ["SPENDGUARD_TEST_ISOLATED"] = "1"
    os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="spendguard-route-realized-")
    os.execv(sys.executable, [sys.executable] + sys.argv)

from spendguard import calls, route_realized


def check(label, cond, failures):
    ok = bool(cond)
    if not ok:
        failures.append(label)
    print(f"  [{'OK' if ok else 'FAIL'}] {label}")


def seed(call_id, ts, *, cost, executor, lane, requested, served,
         fell_from=None, fell_reason=None, redirect_reason=None):
    calls.record_call("openai", served, "realtime", cost, intent="fixture-routing", call_id=call_id,
                      executor=executor, resolved_lane=lane, requested_model=requested, served_model=served,
                      fell_from=fell_from, fell_reason=fell_reason, redirect_reason=redirect_reason)
    with calls._lock:
        calls._calls_db().execute("UPDATE calls SET ts=? WHERE id=?", (ts, call_id))
        calls._calls_db().commit()


def main():
    failures = []

    print("-- seed routed calls --")
    seed("zz-earliest", "2026-06-09T09:00:00", cost=0, executor="codex", lane="codex",
         requested="gpt-5", served="gpt-5")
    seed("yy-api", "2026-06-10T10:00:00", cost=2, executor="api", lane="api",
         requested="gpt-5", served="gpt-5-mini", fell_from="codex", fell_reason="timeout",
         redirect_reason="lane_unavailable")
    seed("xx-legacy", "2026-06-11T11:00:00", cost=0, executor="", lane=None,
         requested="legacy-model", served="legacy-model")
    seed("ww-lane", "2026-06-12T12:00:00", cost=0, executor="gemini", lane="gemini",
         requested="gemini-3", served="gemini-3")
    seed("vv-api", "2026-06-13T13:00:00", cost=3, executor="api", lane="api",
         requested="gpt-5", served="gpt-5", fell_from="codex", fell_reason="timeout",
         redirect_reason="lane_unavailable")
    seed("aa-latest", "2026-06-14T14:00:00", cost=1, executor="api", lane="api",
         requested="gpt-5", served="gpt-5", fell_from="gemini", fell_reason="quota",
         redirect_reason="capacity")

    report = route_realized.route_realized_report("fixture-routing", limit=20)

    print("-- GROUP BY equality and billed/$0 split --")
    with calls._lock:
        direct = calls._calls_db().execute(
            """SELECT COALESCE(NULLIF(executor,''),'unknown'),
                      COALESCE(NULLIF(resolved_lane,''),'unknown'), COUNT(*),
                      SUM(CASE WHEN cost>0 AND COALESCE(NULLIF(executor,''),'') IN ('','api') THEN cost ELSE 0 END)
               FROM calls WHERE intent=? GROUP BY COALESCE(NULLIF(executor,''),'unknown'),
                      COALESCE(NULLIF(resolved_lane,''),'unknown')""", ("fixture-routing",)).fetchall()
    actual = {(r["executor"], r["resolved_lane"]): (r["jobs"], r["billed_cost"])
              for r in report["by_executor_lane"]}
    expected = {(executor, lane): (int(jobs), float(cost)) for executor, lane, jobs, cost in direct}
    check("by_executor_lane equals direct SQL GROUP BY", actual == expected, failures)
    check("three metered API rows", report["totals"]["billed_jobs"] == 3, failures)
    check("metered billed cost is $6", report["totals"]["billed_cost"] == 6.0, failures)
    check("three zero-cost rows", report["totals"]["zero_cost_jobs"] == 3, failures)
    check("metered + zero-lane + unknown shares add to one",
          report["shares"]["metered_share_jobs"] == 0.5
          and report["shares"]["zero_lane_share_jobs"] == 2 / 6
          and report["shares"]["unknown_share_jobs"] == 1 / 6
          and sum((report["shares"][key] for key in
                   ("metered_share_jobs", "zero_lane_share_jobs", "unknown_share_jobs"))) == 1.0,
          failures)

    print("-- honest unknown bucket, falloff, and drift --")
    check("legacy routing is unknown and retained",
          actual[("unknown", "unknown")][0] == 1
          and report["totals"]["unknown_routing_jobs"] == 1,
          failures)
    check("unknown caveat is present", len(report["caveats"]) >= 1, failures)
    reasons = {row["fell_reason"]: row["jobs"] for row in report["fell"]["by_fell_reason"]}
    check("fell_reason exact-string rollup", reasons == {"timeout": 2, "quota": 1}, failures)
    drift = {(row["requested_model"], row["served_model"], row["jobs"])
             for row in report["model_drift"]}
    check("requested/served substitution appears", ("gpt-5", "gpt-5-mini", 1) in drift, failures)
    check("matching requested/served pair is absent",
          all(requested != served for requested, served, _ in drift), failures)

    print("-- ts window and ordering, independent of id --")
    windowed = route_realized.route_realized_report(
        "fixture-routing", since="2026-06-10T00:00:00", as_of="2026-06-13T23:59:59", limit=20)
    sample_ts = [row["ts"] for row in windowed["samples"]]
    check("since/as_of filters ts strings", windowed["totals"]["jobs"] == 4, failures)
    check("samples order by ts descending", sample_ts == sorted(sample_ts, reverse=True), failures)
    check("latest lexical id does not control order",
          report["samples"][0]["ts"] == "2026-06-14T14:00:00", failures)

    empty = route_realized.route_realized_report("no-such-intent")
    check("empty report has honest nulls",
          empty["totals"]["jobs"] == 0
          and empty["samples"] == []
          and empty["shares"]["metered_share_jobs"] is None
          and all(value is None for value in empty["shares"].values()),
          failures)
    check("empty report caveat", len(empty["caveats"]) >= 1, failures)

    failure_count = len(failures)
    print(f"\n{'[FAIL]' if failures else 'OK'} test_route_realized: {failure_count} failure(s)")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()

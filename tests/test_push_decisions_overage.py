"""Phase-2 status aggregates: bounded, scrubbed, reconciled, and offline. Script-style."""
import os
import sys
import tempfile

os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="sg-push-aggregates-")
os.environ.setdefault("SPENDGUARD_TEST_ISOLATED", "1")
os.environ.setdefault("SPENDGUARD_NO_AUTOINSTALL", "1")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from spendguard import guard, overage, pricing, saas, setup  # noqa: E402

fails = []


def ck(label, condition):
    if not condition:
        fails.append(label)
    print(f"  [{'OK' if condition else 'FAIL'}] {label}")


def decision(requested, chosen, cls=None, basis="advisor", n=1, actual=1.0, counterfactual=3.0, saved=2.0):
    return [{"requested_model": requested, "requested_effort": "low", "chosen_model": chosen,
             "chosen_effort": "high", "reason_class": cls, "basis": basis, "actual_usd": actual,
             "counterfactual_usd": counterfactual, "saved_usd": saved} for _ in range(n)]


originals = (guard.decisions_since, saas.saas_connection, saas.contributor_ok, saas.contributor, setup._probe,
             pricing.cross_check_openrouter, guard.shed_summary, guard.decision_rollup,
             overage.current_overage_status)
try:
    rows = (decision("a", "b", "paid-overage-admission", n=2)
            + decision("c", "d", "proactive-shed") + decision("e", "f", "quota-admission")
            + decision("g", "h", basis="best-value") + decision("i", "j") + decision("same", "same")
            + [{"requested_model": None, "chosen_model": None, "basis": "delegate", "actual_usd": 4,
                "counterfactual_usd": 5, "saved_usd": 1}])
    guard.decisions_since = lambda since=None: rows
    summary = guard.decision_rollup(since="2026-10-01")
    ck("decision total includes every row", summary["n"] == 8)
    ck("all bounded reason classes map structurally", {r["reason_class"] for r in summary["by_route"]} == {
        "paid-overage-admission", "proactive-shed", "quota-admission", "best-value", "substitution", "pinned", "other"})
    paid = next(r for r in summary["by_route"] if r["reason_class"] == "paid-overage-admission")
    ck("billed/counterfactual/savings remain three separate sums",
       (paid["n"], paid["billed_usd"], paid["counterfactual_usd"], paid["savings_usd"]) == (2, 2.0, 6.0, 4.0))

    rows = []
    for i in range(26):
        rows += decision(f"requested-{i}", f"chosen-{i}", "substitution", n=26 - i,
                         actual=i + 0.1, counterfactual=i + 0.2, saved=i + 0.3)
    guard.decisions_since = lambda since=None: rows
    summary = guard.decision_rollup()
    remainder = summary["by_route"][-1]
    ck("by_route is capped at 24 with an other rollup", len(summary["by_route"]) == 24
       and remainder["reason_class"] == "other")
    ck("rollup model/effort dimensions are scrubbed nulls", all(remainder[f] is None for f in
       ("requested_model", "requested_effort", "chosen_model", "chosen_effort")))
    ck("rollup count reconciles", sum(r["n"] for r in summary["by_route"]) == summary["n"] == sum(range(1, 27)))
    for output, source in (("billed_usd", "actual_usd"), ("counterfactual_usd", "counterfactual_usd"),
                           ("savings_usd", "saved_usd")):
        ck(f"rollup {output} reconciles", abs(sum(r[output] for r in summary["by_route"])
                                               - sum(r[source] for r in rows)) < 1e-6)

    saas.saas_connection = lambda: {"visibility": "team"}
    saas.contributor_ok = lambda: (True, "ok")
    saas.contributor = lambda: "member"
    setup._probe = lambda executable: ("3", True, True)
    pricing.cross_check_openrouter = lambda: ([], None, None)
    guard.shed_summary = lambda: {"n_sheds": 0}
    guard.decision_rollup = lambda: {"n": 1, "by_route": [{"reason_class": "other"}]}
    overage.current_overage_status = lambda: {
        "on_overage_now": False, "observed_overage_windows": 3,
        "real_overage_by_month_usd": {f"2026-{month:02d}": month + 0.5 for month in range(10, 3, -1)}}
    payload = saas.push_status(dry=True)
    ck("decisions included only as aggregate", payload["decisions"] == {
        "n": 1, "by_route": [{"reason_class": "other"}]})
    ck("overage reshaped and capped at six months", payload["overage"] == {
        "on_overage_now": False, "observed_windows": 3,
        "by_month": [{"month": f"2026-{month:02d}", "billed_overage_usd": month + 0.5}
                     for month in range(10, 4, -1)]})
    ck("empty shed omitted", "shed" not in payload)
    guard.decision_rollup = lambda: {"n": 0, "by_route": []}
    overage.current_overage_status = lambda: {
        "on_overage_now": False, "observed_overage_windows": 9, "real_overage_by_month_usd": {}}
    empty = saas.push_status(dry=True)
    ck("empty decisions and overage omitted", "decisions" not in empty and "overage" not in empty)
finally:
    (guard.decisions_since, saas.saas_connection, saas.contributor_ok, saas.contributor, setup._probe,
     pricing.cross_check_openrouter, guard.shed_summary, guard.decision_rollup,
     overage.current_overage_status) = originals

print(f"\n{'[FAIL]' if fails else 'OK'} test_push_decisions_overage: {len(fails)} failure(s)")
sys.exit(1 if fails else 0)

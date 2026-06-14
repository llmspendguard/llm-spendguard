"""Offline test for cascade routing logic — stub caller + verify, NO network."""
from spendguard import cascade


def check(name, cond):
    print(f"  [{'OK' if cond else 'FAIL'}] {name}")
    assert cond


# stub caller: returns (cost, output) per model; cheap models give "bad", strong gives "good"
COSTS = {"cheap": 0.001, "mid": 0.01, "strong": 0.10}


def caller(model, prompt):
    return COSTS[model], ("good-" + model if model == "strong" else "bad")


print("-- cheap-first, escalate on verify-fail --")
r = cascade.cascade("q", ["cheap", "mid", "strong"],
                    verify=lambda p, o: o.startswith("good"), _caller=caller)
check("ends on strong (only one that verifies)", r["model"] == "strong")
check("escalated through cheap+mid", r["escalations"] == ["cheap", "mid"])
check("cost = sum of all three rungs", abs(r["cost"] - 0.111) < 1e-6)

print("-- cheap wins when it verifies (no escalation, big saving) --")
r = cascade.cascade("q", ["cheap", "mid", "strong"], verify=lambda p, o: True, _caller=caller)
check("served by cheap", r["model"] == "cheap" and not r["escalations"])
check("cost = cheap only", abs(r["cost"] - 0.001) < 1e-9)

print("-- default_verify: empty fails, JSON-task needs valid JSON --")
check("empty output fails", cascade.default_verify("q", "") is False)
check("non-empty prose passes", cascade.default_verify("explain x", "some text") is True)
check("JSON task + broken JSON fails", cascade.default_verify('return JSON {"a":1}', "not json") is False)
check("JSON task + valid JSON passes", cascade.default_verify('return JSON', '{"a":1}') is True)
print("done.")

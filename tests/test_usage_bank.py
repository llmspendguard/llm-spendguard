"""Zero-spend guard tests for the usage-shaped reliability bank."""
import os
import sys
import tempfile
from unittest import mock

os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="sg-usage-bank-")
os.environ["SPENDGUARD_TEST_ISOLATED"] = "1"
os.environ.setdefault("SPENDGUARD_NO_AUTOINSTALL", "1")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))
for imported_name in tuple(sys.modules):
    if imported_name == "spendguard" or imported_name.startswith("spendguard."):
        del sys.modules[imported_name]

from spendguard.analyze.measurement import usage_bank  # noqa: E402
from spendguard.generate.execution import adapters  # noqa: E402


def sample_usage_bank_contract(path=usage_bank.PATH_JSON_STRICT):
    cell = usage_bank.UsageBankCell("openai", path, "low", "gpt-5-nano")
    schema = {"type": "object", "required": ["answer"], "properties": {"answer": {"type": "string"}}}
    return usage_bank.UsageBankSample(cell, "prompt", '{"answer":"ok"}', None, schema, 10, 5)


def main():
    with mock.patch.object(adapters, "call", side_effect=AssertionError("estimate spent")), \
         mock.patch.object(usage_bank, "_cheapest_catalog_model", return_value="gpt-5-nano"), \
         mock.patch.object(usage_bank.pricing, "realtime_cost", return_value=0.01):
        estimate = usage_bank.estimate_usage_bank([sample_usage_bank_contract()], stability_runs=3)
    assert estimate["metered_probes"] == 3
    assert abs(estimate["total_usd"] - 0.03) < 1e-12

    for execute, budget, expected in ((False, 1.0, "--execute"), (True, None, "--budget-usd"),
                                      (True, 0.001, "exceeds budget")):
        try:
            usage_bank.enforce_usage_bank_execution_gate({"total_usd": 0.02}, execute, budget)
        except RuntimeError as error:
            assert expected in str(error)
        else:
            raise AssertionError("metered gate did not refuse")

    passing = sample_usage_bank_contract()
    failing = usage_bank.UsageBankSample(passing.cell, "prompt", "{}", None, passing.schema, 10, 5)
    replay = usage_bank.replay_usage_bank([passing, failing])
    assert replay["covered"] == replay["denominator"] == 1
    assert replay["results"][0]["status"] == usage_bank.STATUS_FAIL

    with mock.patch.object(usage_bank, "_scrub_text", side_effect=lambda text, intent=None: text.replace("secret", "X")):
        payload = {"messages": ["secret", {"tail": "secret"}], "count": 2}
        assert usage_bank.scrub_usage_bank_value(payload) == {
            "messages": ["X", {"tail": "X"}], "count": 2}

    print("OK test_usage_bank: zero-spend estimate + dual execution gate + replay coverage")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

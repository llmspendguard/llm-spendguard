"""Opt-in real-lane fidelity probe. Estimate separately; an explicit approved budget is required.

The probe injects transport failures then calls the real configured executor concurrently.
Paid fallback is forbidden. This is deliberately separate from CI's $0 simulated fault tier.
"""
import argparse
import concurrent.futures
import json
import os
import pathlib
import threading
from unittest.mock import patch

import spendguard
spendguard.require()
from spendguard import adapters, config, lane_recovery, pricing  # noqa: E402


def live_lane_fidelity_main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", required=True)
    parser.add_argument("--estimate", action="store_true")
    parser.add_argument("--live", action="store_true")
    args = parser.parse_args()
    count = lane_recovery.recovery_setting("chaos_callers", 2)
    output = adapters.output_budget(args.model)
    provider = adapters.provider_for(args.model)
    raw = args.model.split(":", 1)[-1]
    value_ceiling = pricing.realtime_cost(raw, len("Reply exactly OK"), output, provider=provider) * count
    estimate = {"model": args.model, "calls": count, "api_usd": 0.0,
                "estimated_subscription_value_ceiling": value_ceiling}
    path = pathlib.Path(config.HOME) / "lane_chaos_live_estimate.json"
    if args.estimate:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(estimate))
        print(json.dumps(estimate, indent=2))
        print("Separate approval required: set SPENDGUARD_CHAOS_LIVE_BUDGET_USD and pass --live. Paid fallback forbidden.")
        return
    budget = float(os.getenv("SPENDGUARD_CHAOS_LIVE_BUDGET_USD", "0"))
    if not args.live or budget <= 0:
        parser.error("live probe requires --live plus an explicit approved SPENDGUARD_CHAOS_LIVE_BUDGET_USD > 0")
    if not path.exists() or json.loads(path.read_text()) != estimate:
        parser.error("first run the separate --estimate command with this model and configuration")
    lane = adapters._lane_for(provider)
    if lane is None:
        parser.error("configured free lane is not ready; no paid probe will be submitted")
    lane_name, executor = lane
    original = executor.run_prompt
    seen = {}
    lock = threading.Lock()

    def inject_live_transport_fault(prompt, **kwargs):
        with lock:
            attempt = seen.get(prompt, 0)
            seen[prompt] = attempt + 1
        if attempt == 0:
            return {"error": "timeout", "transport_error": True}
        return original(prompt, **kwargs)

    def invoke_live_fidelity_caller(n):
        return adapters.call(args.model, f"Reply exactly OK. Probe {n}", intent="chaos:live-fidelity",
                             no_metered_fallback=True, no_substitution=True)

    with patch.object(executor, "run_prompt", side_effect=inject_live_transport_fault):
        with concurrent.futures.ThreadPoolExecutor(max_workers=count) as pool:
            results = list(pool.map(invoke_live_fidelity_caller, range(count)))
    assert all(r.get("executor") == lane_name and r.get("text") and not r.get("error") for r in results), results
    assert all(float(r.get("cost") or 0) == 0 for r in results)
    print(f"GREEN real {lane_name} recovery under concurrency; API billed $0; approved budget ceiling ${budget}")


if __name__ == "__main__":
    live_lane_fidelity_main()

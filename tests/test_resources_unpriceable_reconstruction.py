"""Unknown realtime reconstruction prices refuse spend and preserve missing evidence."""
import contextlib
import json
from unittest.mock import patch

from spendguard import adapters, calls, conv, pricing, resources

RESOURCE_PRICE_UNKNOWN_MODEL = "resource-price-unknown-fixture"
RESOURCE_PRICE_KNOWN_MODEL = "resource-price-known-fixture"
RESOURCE_PRICE_SESSION = "resource-price-session-fixture"
RESOURCE_PRICE_TRANSCRIPT = "full resource price fixture transcript"
RESOURCE_PRICE_ESTIMATE_USD = 1.0
RESOURCE_PRICE_RUN_INPUT = 7
RESOURCE_PRICE_RUN_OUTPUT = 11
RESOURCE_PRICE_REPLIES = (
    {"runs": [{"name": "resource-price-fragment", "kind": "realtime"}]},
    {"runs": [{"name": "resource-price-unknown-run", "model": RESOURCE_PRICE_UNKNOWN_MODEL,
               "total_in": RESOURCE_PRICE_RUN_INPUT, "total_out": RESOURCE_PRICE_RUN_OUTPUT},
              {"name": "resource-price-known-run", "model": RESOURCE_PRICE_KNOWN_MODEL,
               "total_in": RESOURCE_PRICE_RUN_INPUT, "total_out": RESOURCE_PRICE_RUN_OUTPUT}]},
)


def _resource_price_fault_cost(model, *args, **kwargs):
    if model == RESOURCE_PRICE_UNKNOWN_MODEL:
        raise pricing.PriceUnknownError(model)
    return RESOURCE_PRICE_ESTIMATE_USD


def _guard_resource_reconstruction_price_gaps():
    chunks = [(RESOURCE_PRICE_SESSION, RESOURCE_PRICE_TRANSCRIPT)]
    with patch.object(conv, "session_chunks", return_value=chunks), \
            patch.object(pricing, "realtime_cost", side_effect=_resource_price_fault_cost), \
            patch.object(adapters, "call") as adapter:
        refused = resources.reconstruct_realtime_llm_sync(
            run=True, find_model=RESOURCE_PRICE_UNKNOWN_MODEL, consolidate_model=RESOURCE_PRICE_KNOWN_MODEL,
            drop_batch_context=False)
        adapter.assert_not_called()
        assert refused["mode"] == "refused" and refused["complete"] is False
        assert refused["est_find_usd"] is None
        assert refused["gaps"][0]["kind"] == "find_price_unavailable"
    with patch.object(conv, "session_chunks", return_value=chunks), \
            patch.object(conv, "resolve", return_value={}), \
            patch.object(resources, "_norm_model", side_effect=lambda model: model), \
            patch.object(pricing, "realtime_cost", side_effect=_resource_price_fault_cost), \
            patch.object(calls, "context", side_effect=lambda **kwargs: contextlib.nullcontext()), \
            patch.object(adapters, "call", side_effect=[{"text": json.dumps(reply)}
                                                       for reply in RESOURCE_PRICE_REPLIES]):
        result = resources.reconstruct_realtime_llm_sync(
            run=True, find_model=RESOURCE_PRICE_KNOWN_MODEL, consolidate_model=RESOURCE_PRICE_KNOWN_MODEL,
            drop_batch_context=False)
        assert result["complete"] is False and result["total"] is None
        assert result["known_total"] == RESOURCE_PRICE_ESTIMATE_USD
        assert result["gaps"][0]["run"] == RESOURCE_PRICE_REPLIES[-1]["runs"][0]
        assert result["gaps"][0]["kind"] == "run_price_unavailable"
        assert [row["model"] for row in result["runs"]] == [RESOURCE_PRICE_KNOWN_MODEL]
    print("resources unpriceable reconstruction: refusal, named gap, healthy continuation PASS")


if __name__ == "__main__":
    _guard_resource_reconstruction_price_gaps()

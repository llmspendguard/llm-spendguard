"""Comprehend estimates use provider token units and one workload output estimate."""
from spendguard import comprehend


def test_comprehend_estimate_uses_same_workload_units_across_models(monkeypatch):
    tasks = [{"prompt": "complete prompt", "chars": len("evidence") }]
    observed = []
    monkeypatch.setattr(comprehend.lane_balance, "resolve_bulk_output_token_estimate",
                        lambda intent: (37, "configured nominal"))
    monkeypatch.setattr(comprehend, "provider_tokens", type("Tokens", (), {
        "count_text": staticmethod(lambda text, model=None: len(text))}))
    monkeypatch.setattr(comprehend.pricing, "cost_or_unpriced",
                        lambda model, input_tokens, output_tokens, batch=False:
                        observed.append((model, input_tokens, output_tokens)) or 1.0)
    for model in ("catalog-model-a", "catalog-model-b"):
        estimate = comprehend.estimate_corpus(tasks, model, "unit-contract")
        assert estimate["out_tok_per_task"] == 37
        assert estimate["in_tok"] == len("complete prompt")
    assert [row[1:] for row in observed] == [(len("complete prompt"), 37)] * 2

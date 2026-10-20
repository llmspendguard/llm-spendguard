"""Offline contract proof: refresh output carries provenance through the catalog writer."""
import json
import uuid
from pathlib import Path
from types import SimpleNamespace

from _pytest_fixture_bridge import align_test_startup_home, run_pytest_style

align_test_startup_home(prefix=__name__)

from spendguard import bulkgate, model_catalog, refresh  # noqa: E402


def test_refresh_lookup_provenance_reaches_catalog(tmp_path, monkeypatch):
    import anthropic

    model, record = next((model, record) for model, record in model_catalog.all_records().items()
                         if record.get("price", {}).get("verified"))
    price = model_catalog.model_price(model)
    expected = {field: price[field] for field in ("in_", "out", "cached_in", "batch_in", "batch_out")}
    expected.update(source=record["price"]["source"], verified=record["price"]["verified"])
    prompts = []

    def capture_refresh_provenance_request(**request):
        prompts.append(request["messages"][0]["content"])
        return SimpleNamespace(stop_reason="end_turn", content=[
            SimpleNamespace(type="text", text=json.dumps({model: expected}))
        ])

    monkeypatch.setattr(anthropic, "Anthropic", lambda **kwargs: SimpleNamespace(
        messages=SimpleNamespace(create=capture_refresh_provenance_request)))
    monkeypatch.setattr(bulkgate, "maxtokens", lambda signature: {})
    isolated_catalog = tmp_path / Path(model_catalog.DATA_PATH).name
    isolated_catalog.write_bytes(Path(model_catalog.DATA_PATH).read_bytes())
    monkeypatch.setattr(model_catalog, "DATA_PATH", str(isolated_catalog))
    monkeypatch.setattr(model_catalog, "_overlay_path", lambda: None)
    looked = refresh._llm_lookup([model], model, uuid.uuid4().hex)
    assert all(field in prompts[0] for field in ("source", "verified", "cached_in"))
    assert refresh._write(looked) == str(isolated_catalog)
    updated = model_catalog.model_record(model)["price"]
    assert updated["source"] == expected["source"]
    assert updated["verified"] == expected["verified"]
    assert model_catalog.model_price(model)["in_"] == expected["in_"]


if __name__ == "__main__":
    raise SystemExit(run_pytest_style(globals()))

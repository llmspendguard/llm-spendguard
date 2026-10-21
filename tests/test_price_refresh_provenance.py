"""Offline contract proof: refresh output carries provenance through the catalog writer."""
import json
import uuid
from pathlib import Path
from types import SimpleNamespace

from _pytest_fixture_bridge import align_test_startup_home, run_pytest_style

align_test_startup_home(prefix=__name__)

from spendguard import bulkgate, config, model_catalog, refresh  # noqa: E402


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
    original_floor = isolated_catalog.read_bytes()
    monkeypatch.setattr(model_catalog, "DATA_PATH", str(isolated_catalog))
    monkeypatch.setattr(config, "HOME", tmp_path)
    looked = refresh._llm_lookup([model], model, uuid.uuid4().hex)
    assert all(field in prompts[0] for field in ("source", "verified", "cached_in"))
    target = Path(model_catalog.catalog_authoring_path())
    assert refresh._write(looked) == str(target)
    assert target != isolated_catalog and target.is_file()
    assert isolated_catalog.read_bytes() == original_floor
    updated = model_catalog.model_record(model)["price"]
    assert updated["source"] == expected["source"]
    assert updated["verified"] == expected["verified"]
    assert model_catalog.model_price(model)["in_"] == expected["in_"]
    corrupt = target.read_bytes() + target.read_bytes()
    target.write_bytes(corrupt)
    try:
        refresh._write(looked)
    except model_catalog.CatalogLayerCorruptionError:
        pass
    else:
        raise AssertionError("refresh must refuse a corrupt authored catalog")
    assert target.read_bytes() == corrupt


if __name__ == "__main__":
    raise SystemExit(run_pytest_style(globals()))

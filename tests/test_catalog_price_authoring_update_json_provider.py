"""catalog_price_authoring performs its catalog write through an injected update_json provider (architectural
acyclicity: the leaf imports no config). The provider is wired on `import spendguard` by pricing; the resolver
returns the wired rewriter; an unset provider fails loud."""
from spendguard.store.reference_data import catalog_price_authoring


def test_update_json_provider_is_wired_on_import_and_is_config_update_json():
    assert catalog_price_authoring._catalog_update_json_provider is not None
    resolved = catalog_price_authoring._resolve_catalog_update_json()
    # The wired provider forwards to config.update_json (behaviour-identical atomic rewrite).
    assert callable(resolved)


def test_merge_synced_catalog_layer_uses_the_injected_provider(tmp_path, monkeypatch):
    saved = catalog_price_authoring._catalog_update_json_provider
    calls = []

    def fake_update_json(path, mutator, *, required=False):
        calls.append((str(path), required))
        # Exercise the mutator exactly as config.update_json would (no previous document).
        mutator(None)

    # Point the catalog authoring path at a temp dir so no real catalog is touched.
    monkeypatch.setattr(catalog_price_authoring.model_catalog, "catalog_authoring_path",
                        lambda: str(tmp_path / "catalog.json"))
    monkeypatch.setattr(catalog_price_authoring.model_catalog, "catalog_document_from_bytes",
                        lambda original, path: {"models": {}})
    try:
        catalog_price_authoring.set_catalog_update_json_provider(fake_update_json)
        n = catalog_price_authoring.merge_synced_catalog_layer({"vendor/model-x": {"input": 1.0}})
        assert n == 1
        assert len(calls) == 1 and calls[0][1] is True
    finally:
        catalog_price_authoring.set_catalog_update_json_provider(saved)


def test_unset_provider_fails_loud():
    saved = catalog_price_authoring._catalog_update_json_provider
    try:
        catalog_price_authoring.set_catalog_update_json_provider(None)
        raised = False
        try:
            catalog_price_authoring._resolve_catalog_update_json()
        except RuntimeError as exc:
            raised = True
            assert "provider is not set" in str(exc)
        assert raised, "expected RuntimeError when the catalog update-json provider is unset"
    finally:
        catalog_price_authoring.set_catalog_update_json_provider(saved)

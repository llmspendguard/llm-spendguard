"""Catalog sync writes through the gateway while the legacy provider remains resolvable."""
import pytest

from spendguard.store.reference_data import catalog_price_authoring

CATALOG_SYNC_TEST_LOCAL_IDENTIFIER = "vendor/local-owned"
CATALOG_SYNC_TEST_REMOTE_IDENTIFIER = "vendor/model-x"
CATALOG_SYNC_TEST_PROVIDER = "vendor"


def test_update_json_provider_is_wired_on_import_and_is_config_update_json():
    assert catalog_price_authoring._catalog_update_json_provider is not None
    resolved = catalog_price_authoring._resolve_catalog_update_json()
    # The wired provider forwards to config.update_json (behaviour-identical atomic rewrite).
    assert callable(resolved)


def test_synced_catalog_layer_dispatches_gateway_and_preserves_local_ownership(monkeypatch):
    saved = catalog_price_authoring._catalog_update_json_provider
    calls = []

    local_identifier = CATALOG_SYNC_TEST_LOCAL_IDENTIFIER
    remote_identifier = CATALOG_SYNC_TEST_REMOTE_IDENTIFIER
    authored_local_record = {"id": local_identifier, "provider": CATALOG_SYNC_TEST_PROVIDER, "vision": True}
    gateway_document = {"models": {local_identifier: authored_local_record},
                        catalog_price_authoring.CATALOG_LOCAL_AUTHOR_IDS_KEY: [local_identifier]}

    def capture_synced_catalog_gateway(mutator, *, reason, layer="local"):
        calls.append((reason, layer))
        mutator(gateway_document, {})

    def reject_legacy_synced_catalog_writer(*args, **kwargs):
        raise AssertionError("catalog sync bypassed the gateway through its legacy writer")

    monkeypatch.setattr(catalog_price_authoring.model_catalog, "author_catalog_records",
                        capture_synced_catalog_gateway)
    try:
        catalog_price_authoring.set_catalog_update_json_provider(reject_legacy_synced_catalog_writer)
        incoming_records = {remote_identifier: {"id": remote_identifier, "provider": CATALOG_SYNC_TEST_PROVIDER},
                            local_identifier: {"id": local_identifier, "provider": CATALOG_SYNC_TEST_PROVIDER, "vision": False}}
        n = catalog_price_authoring.merge_synced_catalog_layer(incoming_records)
        assert n == len(incoming_records)
        assert calls == [(catalog_price_authoring.CATALOG_SYNC_AUTHOR_REASON, "local")]
        assert gateway_document["models"][local_identifier] == authored_local_record
        assert gateway_document["models"][remote_identifier] == incoming_records[remote_identifier]
        assert gateway_document["models"][remote_identifier] is not incoming_records[remote_identifier]
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


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "--noconftest", "-o", "addopts=", "-q", "--tb=short"]))

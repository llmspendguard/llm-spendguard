"""model_catalog resolves its HOME directory through an injected provider (architectural acyclicity: the leaf
imports no config). The provider is wired on `import spendguard` by pricing; the resolver returns the wired
home; an unset provider fails loud."""
from spendguard.store.reference_data import model_catalog
from spendguard.store.configuration import config


def test_home_provider_is_wired_on_import_and_matches_config_home():
    assert model_catalog._catalog_home_provider is not None
    assert str(model_catalog._resolve_catalog_home()) == str(config.HOME)
    assert model_catalog.catalog_authoring_path().startswith(str(config.HOME))


def test_set_catalog_home_provider_overrides_then_restores(tmp_path):
    saved = model_catalog._catalog_home_provider
    try:
        model_catalog.set_catalog_home_provider(lambda: tmp_path)
        assert str(model_catalog._resolve_catalog_home()) == str(tmp_path)
    finally:
        model_catalog.set_catalog_home_provider(saved)


def test_unset_provider_fails_loud():
    saved = model_catalog._catalog_home_provider
    try:
        model_catalog.set_catalog_home_provider(None)
        raised = False
        try:
            model_catalog.catalog_authoring_path()
        except RuntimeError as exc:
            raised = True
            assert "provider is not set" in str(exc)
        assert raised, "expected RuntimeError when the catalog-home provider is unset"
    finally:
        model_catalog.set_catalog_home_provider(saved)

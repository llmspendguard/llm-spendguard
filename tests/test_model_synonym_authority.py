"""Offline catalog spelling resolution through the model capability authority."""
from unittest.mock import patch

from spendguard import models
from spendguard.store.reference_data import model_catalog


def test_catalog_existing_synonym_capability_authority():
    records = model_catalog._load_records()
    canonical, record = next((key, rec) for key, rec in records.items() if rec.get("aliases"))
    synonym = record["aliases"][0]
    with patch.object(models, "facts", return_value={}):
        assert models.canonical_model_id(synonym) == canonical
        assert models.tokens_param(synonym) == models.tokens_param(canonical)
        assert models.profile(synonym) == models.profile(canonical)
        assert models.reasons_by_default(synonym) == models.reasons_by_default(canonical)


def test_catalog_unknown_synonym_preserves_name():
    unknown = __name__
    assert model_catalog.model_record(unknown) is None
    assert models.canonical_model_id(unknown) == unknown


def test_catalog_retired_synonym_uses_canonical_facts():
    canonical, record = next(iter(model_catalog._load_records().items()))
    retired = __name__ + ".retired"
    snapshot = {canonical: record, retired: {"id": retired, "retired_alias_of": canonical}}
    with patch.object(model_catalog, "_load_records", return_value=snapshot), patch.object(models, "facts", return_value={}):
        assert models.canonical_model_id(retired) == canonical
        assert models.profile(retired) == models.profile(canonical)
        assert models.tokens_param(retired) == models.tokens_param(canonical)


if __name__ == "__main__":
    test_catalog_existing_synonym_capability_authority()
    test_catalog_unknown_synonym_preserves_name()
    test_catalog_retired_synonym_uses_canonical_facts()


def test_send_lookup_uses_canonical_catalog_id():
    from spendguard.generate.execution import adapters
    canonical, record = next((key, rec) for key, rec in model_catalog._load_records().items() if rec.get("aliases"))
    synonym = record["aliases"][0]
    class SynonymLookupCaptured(Exception):
        pass
    captured = []
    def capture_synonym_provider_lookup(model):
        captured.append(model)
        raise SynonymLookupCaptured
    with patch.object(adapters, "provider_for", side_effect=capture_synonym_provider_lookup):
        try:
            adapters._call_once(synonym, "", max_tokens=adapters.TOKEN_FLOOR)
        except SynonymLookupCaptured:
            pass
    assert captured == [canonical], captured


if __name__ == "__main__":
    test_send_lookup_uses_canonical_catalog_id()


def test_selfpriced_retirement_gap_preserves_catalog_identity():
    from spendguard import config
    records = model_catalog._load_records()
    canonical, record = next((key, rec) for key, rec in records.items()
                             if rec.get("retired_alias_of") not in records
                             and rec.get("retired_alias_of") and rec.get("price"))
    with patch.object(config, "warn_once") as gap_warning:
        assert models.canonical_model_id(canonical) == canonical
        assert gap_warning.call_count == 1
        assert "model synonym gap" in str(gap_warning.call_args)


def test_unpriced_retirement_gap_and_cycle_fail_honestly():
    from copy import deepcopy
    records = model_catalog._load_records()
    canonical, record = next(iter(records.items()))
    missing = __name__ + ".missing"
    broken = deepcopy(record)
    broken.pop("price", None)
    broken.pop("price_intervals", None)
    for target in (missing, canonical):
        broken["retired_alias_of"] = target
        with patch.object(model_catalog, "_load_records", return_value={canonical: broken}):
            try:
                models.canonical_model_id(canonical)
            except ValueError as error:
                assert "model synonym gap" in str(error)
            else:
                raise AssertionError("invalid retirement must name its gap")


if __name__ == "__main__":
    test_selfpriced_retirement_gap_preserves_catalog_identity()
    test_unpriced_retirement_gap_and_cycle_fail_honestly()

"""Catalog section readers must preserve explicit corruption refusal signals."""
from unittest.mock import patch

from spendguard import model_catalog

CATALOG_CORRUPTION_GUARD_PAYLOAD = b"{}"
CATALOG_CORRUPTION_GUARD_MESSAGE = "GAP injected_catalog_corruption"
CATALOG_CORRUPTION_SECTION_READERS = ("_parse_models", "_parse_catalog_vendors")


def test_catalog_section_corruption_refusal_survives_fault():
    def inject_catalog_section_corruption(*args, **kwargs):
        raise model_catalog.CatalogLayerCorruptionError(CATALOG_CORRUPTION_GUARD_MESSAGE)

    for section_reader_name in CATALOG_CORRUPTION_SECTION_READERS:
        with patch.object(model_catalog.json, "loads", inject_catalog_section_corruption):
            try:
                getattr(model_catalog, section_reader_name)(CATALOG_CORRUPTION_GUARD_PAYLOAD)
            except model_catalog.CatalogLayerCorruptionError as corruption:
                assert str(corruption) == CATALOG_CORRUPTION_GUARD_MESSAGE
            else:
                raise AssertionError(f"{section_reader_name} swallowed catalog corruption")
    print("PASS: both catalog section readers preserve injected corruption refusals")


if __name__ == "__main__":
    test_catalog_section_corruption_refusal_survives_fault()

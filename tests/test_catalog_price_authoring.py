"""The price command persists provenance to the catalog, even under faults/races."""
import concurrent.futures
import datetime
import json
import os
from pathlib import Path
import tempfile
from unittest.mock import patch

from _pytest_fixture_bridge import align_test_startup_home

align_test_startup_home(prefix=__name__)

from spendguard import catalog_price_authoring, model_catalog  # noqa: E402

AUTHOR_TEST_MODEL = "catalog-authoring-fixture"
AUTHOR_TEST_PROVIDER = "catalog-authoring-provider"
AUTHOR_TEST_SOURCE = "owner-supplied catalog-authoring fixture"
AUTHOR_TEST_CATALOG_NAME = "catalog-authoring.json"
AUTHOR_TEST_HOME_NAME = "isolated-price-author-home"
AUTHOR_TEST_PEERS = ("catalog-authoring-peer-a", "catalog-authoring-peer-b")
AUTHOR_TEST_INPUT_RATE = 7
AUTHOR_TEST_OUTPUT_RATE = 11
AUTHOR_TEST_REVISED_RATE = 13
AUTHOR_TEST_ENCODING = "utf-8"
AUTHOR_TEST_VERIFIED = datetime.datetime.now(datetime.timezone.utc).date().isoformat()


def _run_catalog_authoring_guards():
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        isolated_home = root / AUTHOR_TEST_HOME_NAME
        isolated_home.mkdir()
        catalog = root / AUTHOR_TEST_CATALOG_NAME
        catalog.write_text(json.dumps({"models": {}}), encoding=AUTHOR_TEST_ENCODING)
        floor_snapshot = catalog.read_bytes()
        overlay = isolated_home / catalog.name
        with patch.dict(os.environ, {"SPENDGUARD_HOME": str(isolated_home)}), \
                patch.object(model_catalog, "DATA_PATH", str(catalog)), \
                patch.object(model_catalog, "_overlay_path", return_value=str(overlay)), \
                patch.object(model_catalog, "catalog_authoring_path", return_value=str(overlay)):
            arguments = dict(input=AUTHOR_TEST_INPUT_RATE, output=AUTHOR_TEST_OUTPUT_RATE,
                             provider=AUTHOR_TEST_PROVIDER, source=AUTHOR_TEST_SOURCE,
                             verified=AUTHOR_TEST_VERIFIED)
            path, _ = catalog_price_authoring.set_catalog_model_price(AUTHOR_TEST_MODEL, **arguments)
            assert Path(path) == overlay, "the isolated catalog overlay is the price authoring destination"
            assert model_catalog.model_price(AUTHOR_TEST_MODEL)["in_"] == AUTHOR_TEST_INPUT_RATE
            assert catalog.read_bytes() == floor_snapshot, "authoring must preserve the packaged floor"
            original = overlay.read_bytes()
            for invalid in ({"source": ""}, {"verified": None}, {"verified": "not-an-ISO-date"}):
                try:
                    catalog_price_authoring.set_catalog_model_price(AUTHOR_TEST_MODEL, **(arguments | invalid))
                except ValueError:
                    pass
                else:
                    raise AssertionError("missing/invalid provenance was accepted")
                assert overlay.read_bytes() == original
            with patch.object(catalog_price_authoring.os, "replace", side_effect=InterruptedError):
                try:
                    catalog_price_authoring.set_catalog_model_price(
                        AUTHOR_TEST_MODEL, **(arguments | {"input": AUTHOR_TEST_REVISED_RATE}))
                except InterruptedError:
                    pass
                else:
                    raise AssertionError("injected interrupt was swallowed")
            assert overlay.read_bytes() == original, "interrupt must leave the full original catalog"
            snapshot = Path(str(overlay) + catalog_price_authoring.CATALOG_AUTHOR_BACKUP_SUFFIX)
            assert snapshot.read_bytes() == original
            with concurrent.futures.ThreadPoolExecutor(max_workers=len(AUTHOR_TEST_PEERS)) as pool:
                futures = [pool.submit(catalog_price_authoring.set_catalog_model_price, peer, **arguments)
                           for peer in AUTHOR_TEST_PEERS]
                for future in futures:
                    future.result()
            records = json.loads(overlay.read_bytes())["models"]
            assert set(records) == {AUTHOR_TEST_MODEL, *AUTHOR_TEST_PEERS}, "concurrent edits lost a record"
            assert not list(root.rglob("*" + catalog_price_authoring.CATALOG_AUTHOR_STAGE_SUFFIX))
            from spendguard import cli, gate, pricing
            cli_arguments = ["price", AUTHOR_TEST_MODEL, "--in", str(AUTHOR_TEST_REVISED_RATE),
                             "--out", str(AUTHOR_TEST_OUTPUT_RATE), "--source", AUTHOR_TEST_SOURCE,
                             "--verified", AUTHOR_TEST_VERIFIED]
            with patch("spendguard._load_public_api_and_install_gate"), patch.object(gate, "install"):
                assert cli._dispatch(cli_arguments) == 0
            assert model_catalog.model_price(AUTHOR_TEST_MODEL)["in_"] == AUTHOR_TEST_REVISED_RATE
            assert catalog.read_bytes() == floor_snapshot, "CLI must preserve the packaged floor"
            assert Path(pricing.user_prices_path()) == overlay, "CLI must share the catalog overlay authoring target"
            print("catalog price authoring: SSOT, provenance, interrupt, snapshot, concurrent callers PASS")


if __name__ == "__main__":
    _run_catalog_authoring_guards()

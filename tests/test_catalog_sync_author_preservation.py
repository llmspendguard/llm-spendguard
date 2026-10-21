"""Sync must preserve authored rows, serialize peers, and refuse corrupt shared data."""
import copy
import datetime
import fcntl
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
from unittest.mock import patch

from _pytest_fixture_bridge import align_test_startup_home

align_test_startup_home(prefix=__name__)

from spendguard import catalog_price_authoring as author, config, model_catalog, saas  # noqa: E402

SYNC_AUTHOR_ENCODING = "utf-8"
SYNC_AUTHOR_WAIT_SECONDS = 30
SYNC_AUTHOR_PEERS = 2
SYNC_AUTHOR_CORRUPTION = b'{"models": BROKEN}'
SYNC_AUTHOR_PEER_PROVENANCE_SUFFIX = " concurrent-author"


def _run_sync_author_preservation_guard():
    identifier, source = next((key, value) for key, value in model_catalog.all_records().items()
                              if value.get("price") and value["price"].get("verified"))
    server = copy.deepcopy(source)
    server.pop("price", None)
    today = datetime.datetime.now(datetime.timezone.utc).date().isoformat()
    with TemporaryDirectory(prefix=__name__) as temporary:
        home = Path(temporary)
        with patch.object(config, "HOME", home), patch.object(saas, "_request", return_value={"models": {identifier: server}}):
            target = Path(model_catalog.catalog_authoring_path())
            saas.sync_catalog_overlay()
            author.set_catalog_model_price(identifier, input=source["price"]["in_"],
                                           output=source["price"]["out"], source=source["price"]["source"],
                                           verified=today)
            owned = json.loads(target.read_text(encoding=SYNC_AUTHOR_ENCODING))["models"][identifier]
            saas.sync_catalog_overlay()
            assert json.loads(target.read_text(encoding=SYNC_AUTHOR_ENCODING))["models"][identifier] == owned
            before = target.read_bytes()
            with patch.object(author.os, "replace", side_effect=InterruptedError):
                try:
                    saas.sync_catalog_overlay()
                except InterruptedError:
                    pass
                else:
                    raise AssertionError("sync must propagate interrupted atomic replacement")
            assert target.read_bytes() == before
            assert Path(str(target) + author.CATALOG_AUTHOR_BACKUP_SUFFIX).read_bytes() == before
            assert not list(home.glob("*.tmp"))
            entered, release, peer_started = Event(), Event(), Event()
            replace = author.os.replace

            def pause_catalog_sync_replace(stage, destination):
                if not entered.is_set():
                    entered.set()
                    if not release.wait(SYNC_AUTHOR_WAIT_SECONDS):
                        raise TimeoutError("GAP sync_author_guard_release_deadline")
                return replace(stage, destination)

            def run_catalog_sync_peer():
                peer_started.set()
                return author.set_catalog_model_price(
                    identifier, input=source["price"]["in_"], output=source["price"]["out"],
                    source=source["price"]["source"] + SYNC_AUTHOR_PEER_PROVENANCE_SUFFIX, verified=today)

            with ThreadPoolExecutor(max_workers=SYNC_AUTHOR_PEERS) as pool, patch.object(author.os, "replace", pause_catalog_sync_replace):
                first = pool.submit(saas.sync_catalog_overlay)
                try:
                    assert entered.wait(SYNC_AUTHOR_WAIT_SECONDS)
                    with Path(str(target) + author.CATALOG_AUTHOR_LOCK_SUFFIX).open("a+b") as probe:
                        try:
                            fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        except BlockingIOError:
                            pass
                        else:
                            raise AssertionError("sync must hold the shared author lock through replacement")
                    second = pool.submit(run_catalog_sync_peer)
                    assert peer_started.wait(SYNC_AUTHOR_WAIT_SECONDS)
                finally:
                    release.set()
                first.result(timeout=SYNC_AUTHOR_WAIT_SECONDS)
                second.result(timeout=SYNC_AUTHOR_WAIT_SECONDS)
            final = json.loads(target.read_text(encoding=SYNC_AUTHOR_ENCODING))["models"][identifier]
            assert final["price"]["source"] == source["price"]["source"] + SYNC_AUTHOR_PEER_PROVENANCE_SUFFIX
            target.write_bytes(SYNC_AUTHOR_CORRUPTION)
            try:
                saas.sync_catalog_overlay()
            except model_catalog.CatalogLayerCorruptionError as error:
                assert "catalog_layer_corrupt" in str(error) and str(target) in str(error)
            else:
                raise AssertionError("sync must refuse corrupt authored data")
            assert target.read_bytes() == SYNC_AUTHOR_CORRUPTION


if __name__ == "__main__":
    _run_sync_author_preservation_guard()
    print("PASS catalog sync preserves author ownership, atomic interruption and concurrent peers")

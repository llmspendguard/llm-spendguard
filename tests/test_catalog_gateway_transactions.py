"""Offline lock, partial-publish and process-death catalog transaction proofs."""
import copy
import base64
import json
import multiprocessing
import os

import pytest

from spendguard.store.reference_data import model_catalog

CATALOG_TRANSACTION_TEST_REASON = "offline complete-generation transaction proof"
CATALOG_TRANSACTION_CRASH_STATUS = 73
CATALOG_TRANSACTION_PROCESS_BUDGET = 20
CATALOG_CORRUPT_LAYER_BYTES = b"not a catalog database"


def test_catalog_initial_reader_takes_generation_lock(tmp_path, monkeypatch):
    with model_catalog.catalog_authoring_workspace(tmp_path):
        operations = []
        flock = model_catalog.fcntl.flock

        def record_catalog_generation_lock(descriptor, operation):
            operations.append(operation)
            return flock(descriptor, operation)

        monkeypatch.setattr(model_catalog.fcntl, "flock", record_catalog_generation_lock)
        model_catalog.catalog_seed_parity()
        assert model_catalog.fcntl.LOCK_SH in operations


def test_catalog_recovery_flushes_restoration_before_retiring_journal(tmp_path, monkeypatch):
    with model_catalog.catalog_authoring_workspace(tmp_path):
        paths = model_catalog._catalog_bundle_paths("seed")
        journal = paths[0].with_name(paths[0].name + model_catalog._CATALOG_TRANSACTION_SUFFIX)
        originals = {str(path): base64.b64encode(path.read_bytes()).decode("ascii") for path in paths}
        journal.write_text(json.dumps(originals))
        paths[0].write_bytes(CATALOG_CORRUPT_LAYER_BYTES)
        flushes = []

        def observe_catalog_recovery_flush(directory):
            flushes.append((directory, journal.exists(), paths[0].read_bytes()))

        monkeypatch.setattr(model_catalog, "_catalog_sync_directory", observe_catalog_recovery_flush)
        model_catalog._recover_catalog_bundle(paths)
        expected = base64.b64decode(originals[str(paths[0])])
        assert flushes == [(paths[0].parent, True, expected), (paths[0].parent, False, expected)]


@pytest.mark.parametrize("reader_name", ["all_records", "catalog_floor_document", "unit_price_table"])
def test_catalog_present_corrupt_database_refuses_fallback(tmp_path, reader_name):
    with model_catalog.catalog_authoring_workspace(tmp_path):
        database = model_catalog._catalog_bundle_paths("seed")[2]
        database.write_bytes(CATALOG_CORRUPT_LAYER_BYTES)
        with pytest.raises(model_catalog.CatalogLayerCorruptionError, match="catalog_layer_corrupt"):
            getattr(model_catalog, reader_name)()


def test_catalog_present_unreadable_layer_refuses_fallback(tmp_path, monkeypatch):
    def deny_catalog_open(*args, **kwargs):
        raise PermissionError(CATALOG_TRANSACTION_TEST_REASON)

    monkeypatch.setattr("builtins.open", deny_catalog_open)
    with pytest.raises(PermissionError, match=CATALOG_TRANSACTION_TEST_REASON):
        model_catalog._read_bytes(tmp_path)


def test_catalog_partial_publication_restores_whole_generation(tmp_path, monkeypatch):
    with model_catalog.catalog_authoring_workspace(tmp_path):
        before = model_catalog.catalog_seed_document()
        changed = copy.deepcopy(before)
        identifier = next(iter(changed["models"]))
        changed["models"][identifier]["vision"] = not bool(changed["models"][identifier].get("vision"))
        publish = model_catalog._catalog_atomic_bytes
        seed_path = model_catalog._catalog_bundle_paths("seed")[1]
        failed = False

        def interrupt_catalog_sql_publication(destination, payload):
            nonlocal failed
            if destination == seed_path and not failed:
                failed = True
                raise InterruptedError(CATALOG_TRANSACTION_TEST_REASON)
            return publish(destination, payload)

        monkeypatch.setattr(model_catalog, "_catalog_atomic_bytes", interrupt_catalog_sql_publication)
        with pytest.raises(InterruptedError, match=CATALOG_TRANSACTION_TEST_REASON):
            model_catalog.author_catalog_records(lambda document, snapshot: changed,
                                                reason=CATALOG_TRANSACTION_TEST_REASON, layer="seed")
        assert model_catalog.catalog_seed_document() == before
        assert model_catalog.catalog_floor_document() == before
        assert not model_catalog.catalog_seed_parity()["differing_cells"]


def test_catalog_process_death_recovers_before_runtime_read(tmp_path):
    with model_catalog.catalog_authoring_workspace(tmp_path):
        before = model_catalog.catalog_seed_document()
        source_path = model_catalog._catalog_bundle_paths("seed")[0]

        def crash_catalog_after_json_publication():
            publish = model_catalog._catalog_atomic_bytes

            def publish_catalog_then_exit(destination, payload):
                publish(destination, payload)
                if destination == source_path:
                    os._exit(CATALOG_TRANSACTION_CRASH_STATUS)

            model_catalog._catalog_atomic_bytes = publish_catalog_then_exit

            def alter_catalog_vision(document, snapshot):
                identifier = next(iter(document["models"]))
                document["models"][identifier]["vision"] = not bool(document["models"][identifier].get("vision"))

            model_catalog.author_catalog_records(alter_catalog_vision,
                                                reason=CATALOG_TRANSACTION_TEST_REASON, layer="seed")

        worker = multiprocessing.get_context("fork").Process(target=crash_catalog_after_json_publication)
        worker.start()
        worker.join(CATALOG_TRANSACTION_PROCESS_BUDGET)
        assert not worker.is_alive(), "catalog crash fixture failed to finish"
        assert worker.exitcode == CATALOG_TRANSACTION_CRASH_STATUS
        assert model_catalog.all_records() == before["models"]
        assert not source_path.with_name(source_path.name + model_catalog._CATALOG_TRANSACTION_SUFFIX).exists()
        assert model_catalog.catalog_seed_document() == before
        assert not model_catalog.catalog_seed_parity()["differing_cells"]


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "--noconftest", "-o", "addopts=", "-q", "--tb=short"]))

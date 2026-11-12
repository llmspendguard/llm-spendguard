#!/usr/bin/env python3
"""Build a fresh SQLite catalog artifact from the committed SQL seed.

The binary (catalog.sqlite) is a DISPOSABLE build artifact of the reviewable, committed
catalog_seed.sql — never committed. The wheel builds it via setup.py's build_py; dev and CI
(editable installs) build it on demand through ensure_catalog_database, called by the test
harness entry points (tests/conftest.py and scripts/test/chunked_suite.py).
"""
import argparse
import os
import pathlib
import sqlite3
import tempfile

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
REFERENCE_DATA_DIRECTORY = REPO_ROOT / "src" / "spendguard" / "store" / "reference_data"
DEFAULT_SEED_SOURCE = REFERENCE_DATA_DIRECTORY / "catalog_seed.sql"
DEFAULT_DATABASE_OUTPUT = REFERENCE_DATA_DIRECTORY / "catalog.sqlite"


def build_catalog_database(seed_source, database_output):
    """Build the SQLite artifact from the seed and atomically install it at database_output.

    Writes to a per-call UNIQUE temp file in the destination directory and os.replace()s it into
    place, so concurrent callers (parallel pytest workers, chunked_suite children) converge on
    identical content without colliding on a shared temp path. Returns the emitted model-row count.
    Raises ValueError when the seed yields no models; cleans up the temp file on any failure.
    """
    database_output = pathlib.Path(database_output)
    database_output.parent.mkdir(parents=True, exist_ok=True)
    temporary_fd, temporary_name = tempfile.mkstemp(
        prefix=database_output.name + ".", suffix=".tmp", dir=str(database_output.parent))
    os.close(temporary_fd)
    temporary_output = pathlib.Path(temporary_name)
    try:
        with sqlite3.connect(":memory:") as connection:
            connection.executescript(pathlib.Path(seed_source).read_text(encoding="utf-8"))
            model_count = connection.execute("SELECT COUNT(*) FROM models").fetchone()[0]
            if model_count <= 0:
                raise ValueError("built catalog contains no models")
            temporary_output.write_bytes(connection.serialize())
        temporary_output.replace(database_output)
    except BaseException:
        temporary_output.unlink(missing_ok=True)
        raise
    return model_count


def ensure_catalog_database(seed_source=DEFAULT_SEED_SOURCE, database_output=DEFAULT_DATABASE_OUTPUT):
    """Build the catalog artifact when it is MISSING or STALE (seed newer than the binary); else leave it.

    Idempotent and concurrency-safe (build_catalog_database installs atomically). The freshness test is a
    mechanical mtime comparison, not a content decision. Returns the database_output path.
    """
    seed_source = pathlib.Path(seed_source)
    database_output = pathlib.Path(database_output)
    fresh = (
        database_output.exists()
        and database_output.stat().st_mtime >= seed_source.stat().st_mtime
    )
    if not fresh:
        build_catalog_database(seed_source, database_output)
    return database_output


def build_catalog_database_cli(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--seed", type=pathlib.Path, default=DEFAULT_SEED_SOURCE)
    parser.add_argument("--output", type=pathlib.Path, default=DEFAULT_DATABASE_OUTPUT)
    args = parser.parse_args(argv)
    count = build_catalog_database(args.seed, args.output)
    print(f"built model_rows={count} output={args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(build_catalog_database_cli())

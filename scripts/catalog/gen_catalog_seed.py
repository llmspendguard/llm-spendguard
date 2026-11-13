#!/usr/bin/env python3
"""Generate the deterministic, reviewable catalog seed from authored JSON.

Stage 1 ships the model catalog (models, aliases, lane spellings) and the
catalog_sections breadth. The reasoning_equivalence table is defined in the
schema but is left EMPTY here on purpose: reasoning equivalence is a local,
learned store (store.reference_data.reasoning_equivalence, keyed under
SPENDGUARD_HOME) and is folded in by Stage 2's local tier, never baked into the
shipped seed.
"""
import argparse
import importlib.util
import json
import pathlib

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
SOURCE_ROOT = REPO_ROOT / "src"
REFERENCE_DATA_DIRECTORY = SOURCE_ROOT / "spendguard" / "store" / "reference_data"
PROVENANCE_MODULE_PATH = REFERENCE_DATA_DIRECTORY / "catalog_provenance.py"
_PROVENANCE_SPEC = importlib.util.spec_from_file_location("catalog_provenance", PROVENANCE_MODULE_PATH)
if _PROVENANCE_SPEC is None or _PROVENANCE_SPEC.loader is None:
    raise RuntimeError(f"cannot load provenance constants from {PROVENANCE_MODULE_PATH}")
_PROVENANCE_MODULE = importlib.util.module_from_spec(_PROVENANCE_SPEC)
_PROVENANCE_SPEC.loader.exec_module(_PROVENANCE_MODULE)
PROVENANCE_AUTHORED = _PROVENANCE_MODULE.PROVENANCE_AUTHORED
PROVENANCE_SOURCED = _PROVENANCE_MODULE.PROVENANCE_SOURCED
SEED_RENDERER_MODULE_PATH = REFERENCE_DATA_DIRECTORY / "catalog_seed_rendering.py"
_SEED_RENDERER_SPEC = importlib.util.spec_from_file_location("catalog_seed_rendering", SEED_RENDERER_MODULE_PATH)
if _SEED_RENDERER_SPEC is None or _SEED_RENDERER_SPEC.loader is None:
    raise RuntimeError(f"cannot load seed renderer from {SEED_RENDERER_MODULE_PATH}")
_SEED_RENDERER_MODULE = importlib.util.module_from_spec(_SEED_RENDERER_SPEC)
_SEED_RENDERER_SPEC.loader.exec_module(_SEED_RENDERER_MODULE)
render_catalog_seed = _SEED_RENDERER_MODULE.render_catalog_seed
MODEL_SCALAR_FIELDS = _SEED_RENDERER_MODULE.MODEL_SCALAR_FIELDS
MODEL_SOURCE_FIELDS = _SEED_RENDERER_MODULE.MODEL_SOURCE_FIELDS
MODEL_OPTIONAL_SOURCE_FIELDS = _SEED_RENDERER_MODULE.MODEL_OPTIONAL_SOURCE_FIELDS
DEFAULT_CATALOG_SOURCE = REFERENCE_DATA_DIRECTORY / "model_catalog.json"
DEFAULT_SCHEMA_SOURCE = REFERENCE_DATA_DIRECTORY / "catalog_schema.sql"
DEFAULT_SEED_OUTPUT = REFERENCE_DATA_DIRECTORY / "catalog_seed.sql"


def generate_seed(catalog_source, schema_source, seed_output):
    """Write the deterministic catalog seed SQL from the authored model catalog.

    Inputs (all path-like; read via read_text, destination written via write_text):
      catalog_source -- model_catalog.json, shaped
          {"models": {model_id: {<MODEL_SOURCE_FIELDS>}}, <section>: {...}, ...}.
      schema_source  -- catalog_schema.sql, prepended verbatim ahead of the INSERTs.
      seed_output    -- destination .sql path, overwritten.

    Emits models, model_aliases, model_lane_spellings and catalog_sections rows in a
    fully deterministic order (sorted keys), wrapped in BEGIN IMMEDIATE/COMMIT. The
    reasoning_equivalence table is intentionally left empty -- Stage 2's local tier
    populates it from the learned local store.

    Returns the number of emitted model rows (int).

    Raises ValueError on an empty catalog, a changed model-field contract, or a
    key/id mismatch; AssertionError if the emitted model count diverges from the
    number of source records; and propagates json.JSONDecodeError and OSError from
    reading the sources or writing the output.
    """
    catalog = json.loads(catalog_source.read_text(encoding="utf-8"))
    models = catalog.get("models") or {}
    if not models:
        raise ValueError("catalog source contains no models")
    seed_text = render_catalog_seed(
        catalog, schema_source.read_text(encoding="utf-8"),
        provenance_authored=PROVENANCE_AUTHORED, provenance_sourced=PROVENANCE_SOURCED,
    )
    seed_output.write_text(seed_text, encoding="utf-8")
    return len(models)


def generate_catalog_seed_cli(argv=None):
    """CLI entry point: parse argv, generate the seed, print a one-line summary.

    argv -- argument list; None uses the process arguments (sys.argv[1:]).
    Options (each path-like, defaulting to the reference_data locations):
      --catalog-source, --schema-source, --output.

    Writes the seed file as a side effect and prints
    'emitted model_rows=<n> reasoning_rows=0 (Stage-2 local tier) output=<path>'.
    Returns 0 on success. argparse raises SystemExit on bad arguments; generation
    and file errors from generate_seed propagate.
    """
    parser = argparse.ArgumentParser()
    parser.add_argument("--catalog-source", type=pathlib.Path, default=DEFAULT_CATALOG_SOURCE)
    parser.add_argument("--schema-source", type=pathlib.Path, default=DEFAULT_SCHEMA_SOURCE)
    parser.add_argument("--output", type=pathlib.Path, default=DEFAULT_SEED_OUTPUT)
    args = parser.parse_args(argv)
    model_count = generate_seed(args.catalog_source, args.schema_source, args.output)
    print(f"emitted model_rows={model_count} reasoning_rows=0 (Stage-2 local tier) output={args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(generate_catalog_seed_cli())

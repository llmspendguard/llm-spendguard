"""Build hook for generated package artifacts."""
import importlib.util
import pathlib

from setuptools import setup
from setuptools.command.build_py import build_py

REPOSITORY_ROOT = pathlib.Path(__file__).resolve().parent
CATALOG_BUILDER_PATH = REPOSITORY_ROOT / "scripts" / "catalog" / "build_catalog_db.py"
CATALOG_SEED_PATH = REPOSITORY_ROOT / "src" / "spendguard" / "store" / "reference_data" / "catalog_seed.sql"
CATALOG_PACKAGE_RELATIVE_PATH = pathlib.Path("spendguard") / "store" / "reference_data" / "catalog.sqlite"


def _load_catalog_builder():
    specification = importlib.util.spec_from_file_location("catalog_database_builder", CATALOG_BUILDER_PATH)
    if specification is None or specification.loader is None:
        raise RuntimeError(f"cannot load catalog builder from {CATALOG_BUILDER_PATH}")
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    return module.build_catalog_database


class CatalogBuildingBuildPy(build_py):
    """Build package files, then generate the SQLite artifact in the build tree."""

    def run(self):
        super().run()
        output_path = pathlib.Path(self.build_lib) / CATALOG_PACKAGE_RELATIVE_PATH
        _load_catalog_builder()(CATALOG_SEED_PATH, output_path)


setup(cmdclass={"build_py": CatalogBuildingBuildPy})

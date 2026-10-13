"""Guard that ``model_catalog.json`` names exactly one store: the packaged data SSOT."""
import ast
import os
from pathlib import Path
import sys
import tempfile

os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="sg-catalog-name-")
os.environ.setdefault("SPENDGUARD_TEST_ISOLATED", "1")
os.environ.setdefault("SPENDGUARD_NO_AUTOINSTALL", "1")
ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
PACKAGE = SRC / "spendguard"
sys.path.insert(0, str(SRC))

from spendguard import catalog, model_catalog  # noqa: E402


def _catalog_path_literals():
    hits = []
    for path in PACKAGE.rglob("*.py"):
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                if Path(node.value).name == "model_catalog.json":
                    hits.append((path, node.lineno, node.value))
    return hits


fails = []


def ck(label, condition, detail=""):
    print(f"  [{'OK' if condition else 'FAIL'}] {label}" + (f" — {detail}" if detail and not condition else ""))
    if not condition:
        fails.append(label)


hits = _catalog_path_literals()
expected = (Path(model_catalog.__file__).resolve(), "model_catalog.json")
ck("exactly one Python path literal has basename model_catalog.json", len(hits) == 1, hits)
ck("the sole literal is model_catalog.DATA_PATH", len(hits) == 1 and
   (hits[0][0].resolve(), hits[0][2]) == expected, hits)
ck("catalog.py's live cache uses a different basename",
   Path(catalog.CATALOG_CACHE).name != "model_catalog.json", catalog.CATALOG_CACHE)
files = sorted(SRC.rglob("model_catalog.json"))
ck("exactly one model_catalog.json file exists under src", files == [PACKAGE / "model_catalog.json"], files)

print(f"\n{'[FAIL]' if fails else 'OK'} test_catalog_ssot_single_source: {len(fails)} failure(s)")
sys.exit(1 if fails else 0)

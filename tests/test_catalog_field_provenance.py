"""Guard that every populated published catalog field records its source."""
import os
from pathlib import Path
import sys
import tempfile

os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="sg-catalog-provenance-")
os.environ.setdefault("SPENDGUARD_TEST_ISOLATED", "1")
os.environ.setdefault("SPENDGUARD_NO_AUTOINSTALL", "1")
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from spendguard import model_catalog  # noqa: E402


def _nonempty_string(value):
    return isinstance(value, str) and bool(value.strip())


fails = []
checked = 0
for model_id, record in model_catalog.all_records().items():
    price = record.get("price")
    if price is not None:
        checked += 1
        if not _nonempty_string(price.get("source")):
            fails.append(f"{model_id}: populated price lacks price.source")
    for field in ("output_ceiling", "context_window"):
        published = record.get(field)
        if isinstance(published, dict) and published.get("value") is not None:
            checked += 1
            if not _nonempty_string(published.get("source")):
                fails.append(f"{model_id}: populated {field}.value lacks {field}.source")

for failure in fails:
    print(f"  [FAIL] {failure}")
if not fails:
    print(f"  [OK] all {checked} populated published fields have source provenance")
print(f"\n{'[FAIL]' if fails else 'OK'} test_catalog_field_provenance: {len(fails)} failure(s)")
sys.exit(1 if fails else 0)

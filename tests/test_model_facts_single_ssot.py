"""Offline repo-scan guard against competing authored model fact sources."""
import ast
import json
import os
from pathlib import Path
import re
import sys
import tempfile
from unittest.mock import patch

os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="sg-model-facts-ssot-")
os.environ["SPENDGUARD_NO_AUTOINSTALL"] = "1"
os.environ["SPENDGUARD_TEST_ISOLATED"] = "1"
ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src" / "spendguard"
sys.path.insert(0, str(ROOT / "src"))
# Startup hooks can preload another checkout before the isolated environment is set.
for name in list(sys.modules):
    if name == "spendguard" or name.startswith("spendguard."):
        del sys.modules[name]

from spendguard import model_catalog, pricing  # noqa: E402

fails = []


def ck(label, condition, detail=""):
    print(f"  [{'OK' if condition else 'FAIL'}] {label}" + (f" — {detail}" if detail and not condition else ""))
    if not condition:
        fails.append(label)


RATE_KEYS = {"in_", "out", "batch_in", "cached_in"}
FACT_KEYS = RATE_KEYS | {"output_ceiling", "context_window", "reasoning"}


def model_id_like(key):
    """Mechanical shape: non-metadata identifier with letters and a version/separator."""
    return (
        isinstance(key, str) and not key.startswith("_")
        and bool(re.search(r"[a-zA-Z]", key))
        and bool(re.search(r"[0-9/.-]", key))
    )


def has_model_map(value):
    """Inspect maps inside schema envelopes (catalog.models, providers.*.models)."""
    if not isinstance(value, dict):
        return False
    for key, row in value.items():
        if model_id_like(key) and isinstance(row, dict):
            if FACT_KEYS.intersection(row):
                return True
            if isinstance(row.get("price"), dict) and RATE_KEYS.intersection(row["price"]):
                return True
    return any(has_model_map(child) for child in value.values() if isinstance(child, dict))


authored = []
for path in sorted(SRC.rglob("*.json")):
    rel = path.relative_to(SRC).as_posix()
    try:
        data = json.loads(path.read_text())
    except (ValueError, OSError) as exc:
        ck(f"read JSON: {rel}", False, str(exc))
        continue
    ck(f"no legacy price file: {rel}", rel != "prices.json")
    if has_model_map(data):
        authored.append(rel)
        ck(f"authored model facts: {rel}", rel == "model_catalog.json", "competing SSOT")
ck("exactly one authored model fact file", authored == ["model_catalog.json"], str(authored))

fallback_found = hasattr(pricing, "_FALLBACK")
for path in sorted(SRC.rglob("*.py")):
    source = path.read_text()
    tree = ast.parse(source, filename=str(path))
    # Walk module-level control flow too, but never enter functions or classes.
    pending = list(tree.body)
    while pending:
        node = pending.pop()
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        pending.extend(ast.iter_child_nodes(node))
        if not isinstance(node, ast.Assign) or not isinstance(node.value, ast.Dict):
            continue
        names = [target.id for target in node.targets if isinstance(target, ast.Name)]
        if path == SRC / "pricing.py" and "_FALLBACK" in names:
            fallback_found = True
        count = sum(
            isinstance(key, ast.Constant) and model_id_like(key.value)
            and isinstance(value, ast.Dict)
            and any(isinstance(field, ast.Constant) and field.value in RATE_KEYS for field in value.keys)
            for key, value in zip(node.value.keys, node.value.values)
        )
        if count >= 3:
            ck(f"rate literal: {path.relative_to(SRC)}:{node.lineno}", False, "competing SSOT")
ck("no competing pricing._FALLBACK", not fallback_found)

priced = ceilings = 0
for mid in sorted(model_catalog.all_records()):
    row = model_catalog.model_price(mid)
    if row:
        priced += 1
        with patch.object(pricing, "_vendor_qualified", side_effect=AssertionError("breadth consulted before catalog")):
            try:
                actual = pricing.price(mid)
                ck(f"catalog price authority: {mid}", all(actual[k] == row[k] for k in ("in_", "out")))
            except Exception as exc:
                ck(f"catalog price authority: {mid}", False, str(exc))
    ceiling = model_catalog.published_ceiling(mid)
    if ceiling:
        ceilings += 1
        with patch.object(pricing, "CONTEXT_LIMITS", {mid: {"max_output_tokens": 7, "max_input_tokens": 11}}):
            ck(f"catalog max_output_tokens: {mid}", pricing.max_output_tokens(mid) == ceiling)
            ck(f"catalog output_ceiling: {mid}", pricing.output_ceiling(model_catalog.provider_of(mid), mid, 1) == ceiling)
ck("sample includes catalog prices", priced > 0)
ck("sample includes catalog ceilings", ceilings > 0)

print(f"\n{'[FAIL]' if fails else 'OK'} test_model_facts_single_ssot: {len(fails)} failure(s)")
sys.exit(1 if fails else 0)

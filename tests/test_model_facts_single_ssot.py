"""Offline repo-scan guard against competing authored model fact sources."""
import ast
import json
import os
from pathlib import Path
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


def record_model_fact_ssot_check(failures, label, condition, detail=""):
    print(f"  [{'OK' if condition else 'FAIL'}] {label}" + (f" — {detail}" if detail and not condition else ""))
    if not condition:
        failures.append(label)


RATE_KEYS = {"in_", "out", "batch_in", "cached_in"}
FACT_KEYS = RATE_KEYS | {"output_ceiling", "context_window", "reasoning"}
CATALOG_REQUIRED_RATE_FIELDS = {"in_", "out"}
CATALOG_PUBLISHED_FACT_FIELDS = FACT_KEYS - RATE_KEYS
SSOT_GUARD_FIXTURE_MODELS = ("fixture-model-1", "fixture-model-2", "fixture-model-3")
SSOT_GUARD_FIXTURE_INPUT_RATE = 1.0
SSOT_GUARD_FIXTURE_OUTPUT_RATE = 2.0


def parses_catalog_numeric_rate_contract(value):
    """Parse the reserved rate schema; identifier spelling carries no evidence."""
    return isinstance(value, dict) and CATALOG_REQUIRED_RATE_FIELDS <= value.keys() and all(
        isinstance(value[field], (int, float)) and not isinstance(value[field], bool)
        for field in CATALOG_REQUIRED_RATE_FIELDS
    )


def contains_authored_catalog_contract(value):
    """Find reserved catalog schemas, including records within envelopes."""
    if not isinstance(value, dict):
        return False
    if parses_catalog_numeric_rate_contract(value):
        return True
    if "provider" in value and CATALOG_PUBLISHED_FACT_FIELDS.intersection(value):
        return True
    return any(contains_authored_catalog_contract(child) for child in value.values())


def parses_literal_catalog_rate_contract(node):
    """Parse numeric rate literals from AST without evaluating source code."""
    if not isinstance(node, ast.Dict):
        return False
    fields = {key.value: value for key, value in zip(node.keys, node.values)
              if isinstance(key, ast.Constant) and isinstance(key.value, str)}
    return CATALOG_REQUIRED_RATE_FIELDS <= fields.keys() and all(
        isinstance(fields[field], ast.Constant)
        and isinstance(fields[field].value, (int, float))
        and not isinstance(fields[field].value, bool)
        for field in CATALOG_REQUIRED_RATE_FIELDS
    )


def contains_literal_catalog_rate_contract(node):
    """Inspect every nested literal, independent of map size or model naming."""
    return any(parses_literal_catalog_rate_contract(child) for child in ast.walk(node))


single_rate_fixture = {SSOT_GUARD_FIXTURE_MODELS[0]: {
    "in_": SSOT_GUARD_FIXTURE_INPUT_RATE, "out": SSOT_GUARD_FIXTURE_OUTPUT_RATE}}
unrelated_model_fixture = {identifier: {"in_": identifier, "out": identifier}
                           for identifier in SSOT_GUARD_FIXTURE_MODELS}
for fixture_label, fixture, expected in (
    ("one complete numeric rate record", single_rate_fixture, True),
    ("unrelated model-key dictionary", unrelated_model_fixture, False),
):
    record_model_fact_ssot_check(fails, f"JSON schema guard: {fixture_label}",
                                contains_authored_catalog_contract(fixture) is expected)
    record_model_fact_ssot_check(fails, f"AST schema guard: {fixture_label}",
                                contains_literal_catalog_rate_contract(ast.parse(repr(fixture))) is expected)

# Reintroduce the removed count/spelling proxy: it misses the single real row
# and accepts the unrelated dictionary. Both counterexamples must discriminate.
for fixture_label, fixture, expected in (
    ("single rate omitted", single_rate_fixture, True),
    ("unrelated map accepted", unrelated_model_fixture, False),
):
    legacy_rate_count = sum(isinstance(row, dict) and bool(RATE_KEYS.intersection(row))
                            for row in fixture.values())
    legacy_rate_verdict = legacy_rate_count >= len(SSOT_GUARD_FIXTURE_MODELS)
    record_model_fact_ssot_check(fails, f"count heuristic mutation detected: {fixture_label}",
                                legacy_rate_verdict is not expected)


authored = []
for path in sorted(SRC.rglob("*.json")):
    rel = path.relative_to(SRC).as_posix()
    try:
        data = json.loads(path.read_text())
    except (ValueError, OSError) as exc:
        record_model_fact_ssot_check(fails, f"read JSON: {rel}", False, str(exc))
        continue
    record_model_fact_ssot_check(fails, f"no legacy price file: {rel}", rel != "prices.json")
    if contains_authored_catalog_contract(data):
        authored.append(rel)
        record_model_fact_ssot_check(fails, f"authored model facts: {rel}", rel == "model_catalog.json", "competing SSOT")
record_model_fact_ssot_check(fails, "exactly one authored model fact file", authored == ["model_catalog.json"], str(authored))

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
        if contains_literal_catalog_rate_contract(node.value):
            record_model_fact_ssot_check(fails, f"rate literal: {path.relative_to(SRC)}:{node.lineno}", False, "competing SSOT")
record_model_fact_ssot_check(fails, "no competing pricing._FALLBACK", not fallback_found)

priced = ceilings = 0
for mid in sorted(model_catalog.all_records()):
    row = model_catalog.model_price(mid)
    if row:
        priced += 1
        with patch.object(pricing, "_vendor_qualified", side_effect=AssertionError("breadth consulted before catalog")):
            try:
                actual = pricing.price(mid)
                record_model_fact_ssot_check(fails, f"catalog price authority: {mid}", all(actual[k] == row[k] for k in ("in_", "out")))
            except Exception as exc:
                record_model_fact_ssot_check(fails, f"catalog price authority: {mid}", False, str(exc))
    ceiling = model_catalog.published_ceiling(mid)
    if ceiling:
        ceilings += 1
        with patch.object(pricing, "CONTEXT_LIMITS", {mid: {"max_output_tokens": 7, "max_input_tokens": 11}}):
            record_model_fact_ssot_check(fails, f"catalog max_output_tokens: {mid}", pricing.max_output_tokens(mid) == ceiling)
            record_model_fact_ssot_check(fails, f"catalog output_ceiling: {mid}", pricing.output_ceiling(model_catalog.provider_of(mid), mid, 1) == ceiling)
record_model_fact_ssot_check(fails, "sample includes catalog prices", priced > 0)
record_model_fact_ssot_check(fails, "sample includes catalog ceilings", ceilings > 0)

print(f"\n{'[FAIL]' if fails else 'OK'} test_model_facts_single_ssot: {len(fails)} failure(s)")
sys.exit(1 if fails else 0)

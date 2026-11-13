"""Offline whole-catalog completeness proof through the public gateway."""
import datetime
import copy
import math
from pathlib import Path
import unittest

import pytest

from spendguard import model_catalog

CATALOG_AUDIT_EMPTY_VALUES = (None, "", [], {})
CATALOG_AUDIT_NON_CHAT_MODES = ("embedding",)
CATALOG_AUDIT_CHAT_FIELDS = ("output_ceiling", "reasoning", "vision")
CATALOG_AUDIT_EXEMPT_FIELDS = ("metered_id", "price", "output_ceiling", "context_window", "reasoning", "vision")
CATALOG_AUDIT_ZERO = 0


def read_catalog_audit_inputs():
    """Read complete authored and deployed-floor snapshots using logical gateway APIs."""
    contract = model_catalog.MODEL_CATALOG_COMPLETENESS_CONTRACT
    authored = model_catalog.catalog_seed_document()["models"]
    runtime = model_catalog.catalog_floor_document()["models"]
    legacy_shape_problems = model_catalog.validate_catalog(runtime)
    assert not legacy_shape_problems, "\n".join(legacy_shape_problems)
    return contract, authored, runtime


def catalog_audit_path_cell(record, path):
    """Resolve a declared path without dropping missing descendants from the census."""
    parent = record
    components = path.split(".")
    for component in components[:-1]:
        parent = parent.get(component) if isinstance(parent, dict) else None
    present = isinstance(parent, dict) and components[-1] in parent
    return present, parent[components[-1]] if present else None


def catalog_audit_path_applies(record, path, contract):
    """Apply only explicitly declared structural and billing exemptions."""
    components = path.split(".")
    root = components[0]
    if record.get("billing_exempt_reason") and root in CATALOG_AUDIT_EXEMPT_FIELDS:
        return False
    capabilities = record.get("capabilities")
    mode = capabilities.get("mode") if isinstance(capabilities, dict) else None
    if mode in CATALOG_AUDIT_NON_CHAT_MODES and root in CATALOG_AUDIT_CHAT_FIELDS:
        return False
    for depth in range(1, len(components)):
        ancestor = ".".join(components[:depth])
        if contract[ancestor][1] == "optional":
            present, value = catalog_audit_path_cell(record, ancestor)
            if not present or value is None:
                return False
    return True


def catalog_audit_cell_census(records, contract):
    """Account for every model/path cell, including descendants of absent parents."""
    census = {path: {"denominator": len(records), "present": 0, "missing": 0,
                     "empty": 0, "required_gaps": 0} for path in contract}
    for record in records.values():
        for path, (_, policy) in contract.items():
            present, value = catalog_audit_path_cell(record, path)
            counts = census[path]
            counts["present" if present else "missing"] += 1
            if present and value in CATALOG_AUDIT_EMPTY_VALUES:
                counts["empty"] += 1
            if catalog_audit_path_applies(record, path, contract) and (
                    policy == "required" and (not present or value in CATALOG_AUDIT_EMPTY_VALUES)
                    or policy == "empty_allowed" and not present):
                counts["required_gaps"] += 1
    return census


def inspect_catalog_audit_cells(records, contract):
    """Report missing requirements separately from malformed present cells; never infer semantics."""
    gaps, malformed = [], []
    for model_id, record in sorted(records.items()):
        if not isinstance(record, dict):
            malformed.append(f"{model_id}: record is not an object")
            continue
        if record.get("id") != model_id:
            malformed.append(f"{model_id}: id/key mismatch")
        unknown = set(record) - {path.split(".")[0] for path in contract}
        malformed.extend(f"{model_id}: undeclared field {key}" for key in sorted(unknown))
        for parent_path in {path.rsplit(".", 1)[0] for path in contract if "." in path}:
            node = record
            for component in parent_path.split("."):
                node = node.get(component) if isinstance(node, dict) else None
            if isinstance(node, dict):
                allowed = {path[len(parent_path) + 1:].split(".")[0]
                           for path in contract if path.startswith(parent_path + ".")}
                malformed.extend(f"{model_id}: undeclared field {parent_path}.{key}"
                                 for key in sorted(set(node) - allowed))
        for path, (kind, policy) in contract.items():
            present, value = catalog_audit_path_cell(record, path)
            applicable = catalog_audit_path_applies(record, path, contract)
            if not present or value in CATALOG_AUDIT_EMPTY_VALUES:
                if applicable and (policy == "required" or (policy == "empty_allowed" and not present)):
                    gaps.append(f"{model_id}: {path} ({'missing' if not present else 'empty/null'})")
                continue
            valid = {
                "str": lambda value=value: isinstance(value, str) and bool(value.strip()),
                "bool": lambda value=value: type(value) is bool,
                "number": lambda value=value: type(value) in (int, float) and math.isfinite(value)
                and value >= CATALOG_AUDIT_ZERO,
                "positive_int": lambda value=value: type(value) is int and value > CATALOG_AUDIT_ZERO,
                "dict": lambda value=value: isinstance(value, dict),
                "list": lambda value=value: isinstance(value, list)
                and all(isinstance(v, str) and v.strip() for v in value),
                "interval_list": lambda value=value: isinstance(value, list)
                and all(isinstance(v, dict) for v in value),
                "date": lambda value=value: isinstance(value, str)
                and datetime.date.fromisoformat(value).isoformat() == value,
            }
            try:
                cell_valid = valid[kind]()
            except (ValueError, TypeError):
                cell_valid = False
            if not cell_valid:
                malformed.append(f"{model_id}: {path} invalid {kind}")
            if path == "lane_spellings" and isinstance(value, dict):
                for lane, spellings in value.items():
                    if not isinstance(lane, str) or not isinstance(spellings, list) or not spellings \
                            or any(not isinstance(v, str) or not v.strip() for v in spellings):
                        malformed.append(f"{model_id}: lane_spellings.{lane} invalid spelling list")
    return gaps, malformed


class ModelCatalogCompletenessProof(unittest.TestCase):
    def test_catalog_audit_shapes_and_floor_parity(self):
        contract, authored, runtime = read_catalog_audit_inputs()
        self.assertTrue(authored, "empty catalog is not completeness")
        self.assertEqual(authored, runtime, "JSON and actual seed floor disagree")
        for label, records in (("authored", authored), ("runtime", runtime)):
            gaps, malformed = inspect_catalog_audit_cells(records, contract)
            print(f"{label}: denominator={len(records)} models; schema={len(contract)} paths; gaps={len(gaps)}")
            census = catalog_audit_cell_census(records, contract)
            self.assertEqual(sum(cell["required_gaps"] for cell in census.values()), len(gaps))
            for path, counts in census.items():
                self.assertEqual(counts["present"] + counts["missing"], len(records), path)
            self.assertEqual(malformed, [], "\n".join(malformed))

    def test_catalog_audit_mutations_are_detected(self):
        contract, authored, runtime = read_catalog_audit_inputs()
        for path, (_, policy) in contract.items():
            if policy != "required" or "." in path:
                continue
            model_id = next(key for key, record in runtime.items()
                            if path in record and catalog_audit_path_applies(record, path, contract))
            altered = copy.deepcopy(runtime)
            altered[model_id].pop(path)
            gaps, malformed = inspect_catalog_audit_cells(altered, contract)
            missing_gap = f"{model_id}: {path} (missing)"
            self.assertNotIn(missing_gap, inspect_catalog_audit_cells(runtime, contract)[0], path)
            self.assertIn(missing_gap, gaps, path)
        model_id = next(iter(runtime))
        altered = copy.deepcopy(runtime)
        altered[model_id]["as_of"] = model_id
        self.assertTrue(inspect_catalog_audit_cells(altered, contract)[1])
        altered = copy.deepcopy(runtime)
        altered[model_id]["aliases"] = [None]
        self.assertTrue(inspect_catalog_audit_cells(altered, contract)[1])

    def test_catalog_audit_missing_subtree_retains_each_required_descendant(self):
        contract, _, runtime = read_catalog_audit_inputs()
        required_paths = [path for path, (_, policy) in contract.items()
                          if policy == "required" and (path == "context_window"
                                                       or path.startswith("context_window."))]
        model_id, record = next((key, candidate) for key, candidate in runtime.items()
                                if all(catalog_audit_path_cell(candidate, path)[1]
                                       not in CATALOG_AUDIT_EMPTY_VALUES for path in required_paths)
                                and not candidate.get("billing_exempt_reason"))
        altered = copy.deepcopy(record)
        altered.pop("context_window", None)
        altered.pop("billing_exempt_reason", None)
        gaps, malformed = inspect_catalog_audit_cells({model_id: altered}, contract)
        for path in required_paths:
            self.assertTrue(any(gap.startswith(f"{model_id}: {path} (") for gap in gaps), path)
        self.assertEqual(malformed, [])

    @pytest.mark.xfail(strict=True, reason="G0 audit: required catalog facts remain unfilled")
    def test_catalog_audit_required_cells_reproduce_gap(self):
        """Strict expected failure: docs/MODEL_CATALOG_SSOT_AUDIT.md; phase 1 must remove this marker."""
        contract, authored, runtime = read_catalog_audit_inputs()
        gaps, _ = inspect_catalog_audit_cells(runtime, contract)
        print("Required unfilled cells (whole runtime floor):\n" + "\n".join(gaps))
        self.assertEqual(gaps, [], "\n".join(gaps))


if __name__ == "__main__":
    raise SystemExit(pytest.main([str(Path(__file__).resolve()), "-q", "-s", "--noconftest",
                                 "-o", "addopts=", "-o", "python_files=test_model_catalog_completeness.py"]))

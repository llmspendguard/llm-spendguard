"""Maintenance catalog rewrites preserve original bytes under interrupted writes."""
import copy
import json
from pathlib import Path
import tempfile
import types
from unittest.mock import patch

from _pytest_fixture_bridge import align_test_startup_home

align_test_startup_home(prefix=__name__)

from spendguard import pricing  # noqa: E402

MAINTENANCE_SCRIPT_DIRECTORY = Path(__file__).resolve().parents[1] / "scripts" / "migrate"
MAINTENANCE_SCRIPT_STEMS = (
    "catalog_provenance_and_bases", "fix_catalog_provenance_and_glm_ceilings", "seed_model_catalog",
)
MAINTENANCE_ENCODING = "utf-8"
MAINTENANCE_JSON_INDENT = 2
MAINTENANCE_FAULT_DIVISOR = 2
MAINTENANCE_PAYLOAD_FIELD = "maintenance_guard_payload"


def _load_catalog_maintenance_guard_module(stem, mutate_writer=False):
    path = MAINTENANCE_SCRIPT_DIRECTORY / (stem + ".py")
    source = path.read_text(encoding=MAINTENANCE_ENCODING)
    if mutate_writer:
        sink = "CATALOG" if stem == MAINTENANCE_SCRIPT_STEMS[0] else "DEST"
        payload = {MAINTENANCE_SCRIPT_STEMS[0]: "cat", MAINTENANCE_SCRIPT_STEMS[1]: "doc",
                   MAINTENANCE_SCRIPT_STEMS[2]: "data"}[stem]
        old = f"config.update_json({sink}, lambda current: {payload}, required=True)"
        assert source.count(old) == 1, "atomic writer mutation location changed"
        source = source.replace(old, f"Path({sink}).write_text(json.dumps({payload}))")
    module = types.ModuleType(stem)
    module.__file__ = str(path)
    module.__dict__["Path"] = Path
    exec(compile(source, str(path), "exec"), module.__dict__)
    return module


def _assert_catalog_maintenance_fault_preservation(module):
    original_write = Path.write_text
    with tempfile.TemporaryDirectory(prefix=__name__) as temporary:
        target = Path(temporary) / (module.__name__ + ".json")
        bases = getattr(module, "PROVIDER_BASE", {})
        document = {MAINTENANCE_PAYLOAD_FIELD: list(MAINTENANCE_SCRIPT_STEMS), "models": {
            mid: {"id": mid, "provider": provider, "context_window": len(mid),
                  "price": {"in_": len(mid), "source": pricing.PRICING_SOURCE}}
            for provider, mid in bases.items()}}
        original_write(target, json.dumps(document, indent=MAINTENANCE_JSON_INDENT))
        original = target.read_bytes()
        sink = "CATALOG" if hasattr(module, "CATALOG") else "DEST"

        def interrupt_catalog_maintenance_output(path, data, *args, **kwargs):
            if path == target or path.suffix == ".tmp":
                original_write(path, data[:len(data) // MAINTENANCE_FAULT_DIVISOR], *args, **kwargs)
                raise InterruptedError("injected maintenance serialization interruption")
            return original_write(path, data, *args, **kwargs)

        with patch.object(module, sink, str(target)), \
                patch.object(module, "build", return_value=copy.deepcopy(document), create=True):
            arguments = ([] if hasattr(module, "CATALOG") else [["--write", "--force"]
                         if module.__name__ == MAINTENANCE_SCRIPT_STEMS[2] else ["--write"]])
            with patch.object(Path, "write_text", interrupt_catalog_maintenance_output):
                try:
                    module.main(*arguments)
                except InterruptedError:
                    pass
                else:
                    raise AssertionError("maintenance write fault was not propagated")
            assert target.read_bytes() == original, "interruption damaged the original catalog"
            assert any(path.read_bytes() == original for path in target.parent.glob("*.bak*")), "backup missing"
            module.main(*arguments)
            written = json.loads(target.read_bytes())
            assert written[MAINTENANCE_PAYLOAD_FIELD] == document[MAINTENANCE_PAYLOAD_FIELD]
            assert set(document["models"]) <= set(written["models"])
            assert target.read_text().count("\n") > MAINTENANCE_JSON_INDENT


for maintenance_script_stem in MAINTENANCE_SCRIPT_STEMS:
    _assert_catalog_maintenance_fault_preservation(_load_catalog_maintenance_guard_module(maintenance_script_stem))
    try:
        _assert_catalog_maintenance_fault_preservation(
            _load_catalog_maintenance_guard_module(maintenance_script_stem, mutate_writer=True))
    except AssertionError as maintenance_mutation_failure:
        assert "damaged the original catalog" in str(maintenance_mutation_failure)
    else:
        raise AssertionError("direct maintenance writer mutation escaped the guard")

print("catalog maintenance atomicity: all writers preserve bytes under interruption; direct-write mutations FAIL")

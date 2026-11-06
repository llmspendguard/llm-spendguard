"""Phase 2a domain-context skeleton, registry, and lazy-surface proofs."""

import importlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
from types import ModuleType

from spendguard.domain_context_surface import install_domain_context_surface


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
PACKAGE_ROOT = REPOSITORY_ROOT / "src" / "spendguard"
REGISTRY_PATH = PACKAGE_ROOT / "domain_context_registry.json"
EXPECTED_CONTEXT_COUNT = 21
EXPECTED_KIND_COUNT = 7
EXPECTED_UNIQUE_PACKAGE_DIRECTORY_COUNT = 25
EXPECTED_TAXONOMY_CORPUS_COUNT = 233
EXPECTED_LIVE_CORPUS_COUNT = 235
FIXTURE_BACKING_MODULE_NAME = "domain_context_lazy_fixture_backing"
FIXTURE_PUBLISHED_NAME = "public_value"
FIXTURE_BACKING_ATTRIBUTE_NAME = "BACKING_VALUE"
FIXTURE_BACKING_VALUE = "resolved-only-on-access"
FIXTURE_MISSING_ATTRIBUTE_NAME = "missing_value"
FIXTURE_PRIVATE_PUBLISHED_NAME = "_private_value"
SANCTIONED_INFRASTRUCTURE_GAPS = {"domain_context_surface", "domain_legacy_path_shim"}
CORRECTED_CONFIGURATION_MODULE = "store.configuration.config"
CORRECTED_TOKEN_OPERATIONS_MODULE = "manage.token_operations.execution_journal"


def load_domain_context_registry():
    return json.loads(REGISTRY_PATH.read_text())


def write_lazy_fixture_module(directory):
    fixture_path = Path(directory) / f"{FIXTURE_BACKING_MODULE_NAME}.py"
    fixture_path.write_text(f"{FIXTURE_BACKING_ATTRIBUTE_NAME} = {FIXTURE_BACKING_VALUE!r}\n")


def test_lazy_surface_resolves_only_on_access():
    with tempfile.TemporaryDirectory() as directory:
        write_lazy_fixture_module(directory)
        sys.path.insert(0, directory)
        sys.modules.pop(FIXTURE_BACKING_MODULE_NAME, None)
        try:
            fixture_module = ModuleType("domain_context_lazy_fixture")
            install_domain_context_surface(
                fixture_module.__dict__,
                {
                    FIXTURE_PUBLISHED_NAME:
                        f"{FIXTURE_BACKING_MODULE_NAME}:{FIXTURE_BACKING_ATTRIBUTE_NAME}"
                },
            )
            assert FIXTURE_BACKING_MODULE_NAME not in sys.modules
            assert getattr(fixture_module, FIXTURE_PUBLISHED_NAME) == FIXTURE_BACKING_VALUE
            assert FIXTURE_BACKING_MODULE_NAME in sys.modules
            assert fixture_module.__dict__[FIXTURE_PUBLISHED_NAME] == FIXTURE_BACKING_VALUE
        finally:
            sys.path.remove(directory)
            sys.modules.pop(FIXTURE_BACKING_MODULE_NAME, None)


def test_lazy_surface_bogus_mapping_raises_attribute_error():
    fixture_module = ModuleType("domain_context_bogus_fixture")
    install_domain_context_surface(
        fixture_module.__dict__,
        {
            FIXTURE_PUBLISHED_NAME:
                f"{FIXTURE_BACKING_MODULE_NAME}:{FIXTURE_MISSING_ATTRIBUTE_NAME}"
        },
    )
    try:
        getattr(fixture_module, FIXTURE_PUBLISHED_NAME)
    except AttributeError:
        pass
    else:
        raise AssertionError("a bogus published target must raise AttributeError")


def test_lazy_surface_refuses_private_published_name():
    fixture_module = ModuleType("domain_context_private_fixture")
    try:
        install_domain_context_surface(
            fixture_module.__dict__,
            {
                FIXTURE_PRIVATE_PUBLISHED_NAME:
                    f"{FIXTURE_BACKING_MODULE_NAME}:{FIXTURE_BACKING_ATTRIBUTE_NAME}"
            },
        )
    except ValueError:
        pass
    else:
        raise AssertionError("a private published name must be refused")


def test_registry_packages_exist_and_import_as_their_surfaces():
    registry = load_domain_context_registry()
    contexts = registry["contexts"]
    assert len(contexts) == EXPECTED_CONTEXT_COUNT
    assert len({context["kind"] for context in contexts}) == EXPECTED_KIND_COUNT
    for context in contexts:
        package_parts = context["package"].split(".")[1:]
        package_path = PACKAGE_ROOT.joinpath(*package_parts)
        assert package_path.is_dir(), context["package"]
        assert (package_path / "__init__.py").is_file(), context["package"]
        imported_surface = importlib.import_module(context["package"])
        assert imported_surface.__name__ == context["surface_module"]
        assert imported_surface.__all__ == context["published_names"]
        assert imported_surface.__all__ == sorted(imported_surface.__all__)
        assert all(not published_name.startswith("_") for published_name in imported_surface.__all__)
    created_directories = {
        path for path in PACKAGE_ROOT.rglob("*")
        if path.is_dir() and (path / "__init__.py").is_file()
        and path.relative_to(PACKAGE_ROOT).parts[0] in {
            "store", "generate", "manage", "analyze", "egress", "platform", "interface"
        }
    }
    assert len(created_directories) == EXPECTED_UNIQUE_PACKAGE_DIRECTORY_COUNT


def test_kind_namespace_imports_no_spendguard_children():
    kind_packages = ("store", "generate", "manage", "analyze")
    probe = """
import importlib, json, sys
sys.path.insert(0, sys.argv[2])
import spendguard
baseline = set(sys.modules)
package = importlib.import_module(sys.argv[1])
added = sorted(name for name in set(sys.modules) - baseline if name.startswith('spendguard.'))
print(json.dumps({'package': package.__name__, 'added': added}))
"""
    for kind_package in kind_packages:
        dotted_package = f"spendguard.{kind_package}"
        completed = subprocess.run(
            [sys.executable, "-S", "-c", probe, dotted_package, str(PACKAGE_ROOT.parent)],
            cwd=REPOSITORY_ROOT,
            check=True,
            capture_output=True,
            text=True,
        )
        result = json.loads(completed.stdout.strip().splitlines()[-1])
        assert result["package"] == dotted_package
        assert all(added_module == dotted_package for added_module in result["added"])


def test_registry_partitions_every_top_level_module_and_pins_corrections():
    registry = load_domain_context_registry()
    corpus = {
        ".".join(path.relative_to(PACKAGE_ROOT).with_suffix("").parts)
        for path in PACKAGE_ROOT.rglob("*.py")
        if path == PACKAGE_ROOT / "__init__.py" or path.name != "__init__.py"
    }
    assignments = {}
    for context in registry["contexts"]:
        assert context["published_names"] == sorted(context["published_names"])
        assert all(not published_name.startswith("_") for published_name in context["published_names"])
        for module_name in context["backing_modules"]:
            assert module_name not in assignments, module_name
            assignments[module_name] = context["id"]
    gaps = {entry["module"]: entry["reason"] for entry in registry["unassigned_modules"]}
    assert all(reason.strip() for reason in gaps.values())
    assert set(assignments).isdisjoint(gaps)
    assert set(assignments) | set(gaps) == corpus
    assert len(assignments) == EXPECTED_TAXONOMY_CORPUS_COUNT
    assert len(corpus) == EXPECTED_LIVE_CORPUS_COUNT
    assert gaps.keys() == SANCTIONED_INFRASTRUCTURE_GAPS
    assert assignments[CORRECTED_CONFIGURATION_MODULE] == "configuration"
    assert assignments[CORRECTED_TOKEN_OPERATIONS_MODULE] == "token_operations"


def test_context_imports_leave_sdk_and_gate_state_unchanged():
    registry = load_domain_context_registry()
    context_packages = [context["package"] for context in registry["contexts"]]
    probe = """
import importlib, json, sys
sys.path.insert(0, sys.argv[2])
import spendguard

def method_state(module_name, class_name):
    try:
        module = importlib.import_module(module_name)
        method = getattr(getattr(module, class_name), 'create')
    except ModuleNotFoundError:
        return 'absent'
    return 'gated' if getattr(method, '_spend_gated', False) else 'ungated'

def state():
    gate = sys.modules.get('spendguard.gate')
    return {
        'openai': method_state('openai.resources.files', 'Files'),
        'anthropic': method_state('anthropic.resources.messages.batches', 'Batches'),
        'gate_installed': bool(gate is not None and gate._enforcement_armed()),
        'gate_unarmed_details': [] if gate is None else gate._unarmed_sdk_details(),
    }

before = state()
for package_name in json.loads(sys.argv[1]):
    importlib.import_module(package_name)
print(json.dumps({'before': before, 'after': state()}, sort_keys=True))
"""
    completed = subprocess.run(
        [
            sys.executable, "-S", "-c", probe, json.dumps(context_packages),
            str(PACKAGE_ROOT.parent),
        ],
        cwd=REPOSITORY_ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    result = json.loads(completed.stdout.strip().splitlines()[-1])
    assert result["after"] == result["before"]


test_lazy_surface_resolves_only_on_access()
test_lazy_surface_bogus_mapping_raises_attribute_error()
test_lazy_surface_refuses_private_published_name()
test_registry_packages_exist_and_import_as_their_surfaces()
test_kind_namespace_imports_no_spendguard_children()
test_registry_partitions_every_top_level_module_and_pins_corrections()
test_context_imports_leave_sdk_and_gate_state_unchanged()
print("[OK] domain context skeleton, registry, lazy surfaces, and zero-side-effect imports")

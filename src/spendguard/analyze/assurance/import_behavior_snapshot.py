"""Deterministic, offline snapshots of import-time spend-gate behaviour."""

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

from ... import config


IMPORT_BEHAVIOR_SNAPSHOT_RESOURCE = "import_behavior_snapshot.json"
IMPORT_BEHAVIOR_PACKAGE_RELATIVE_ROOT = Path("src") / "spendguard"
IMPORT_BEHAVIOR_SCHEMA_VERSION = 1
IMPORT_BEHAVIOR_SUBPROCESS_TIMEOUT_SECONDS = 30
IMPORT_BEHAVIOR_PROBE_PREFIX = "spendguard-import-behavior-"
IMPORT_BEHAVIOR_RESULT_PREFIX = "SPENDGUARD_IMPORT_BEHAVIOR="
IMPORT_BEHAVIOR_MODULE_PREFIX = "spendguard."
IMPORT_BEHAVIOR_OFFLINE_COMMAND = "__import_behavior_snapshot__"
IMPORT_BEHAVIOR_ENTRY_POINTS = (
    "package_import",
    "module_main",
    "sitecustomize_hook",
    "usercustomize_hook",
    "sdk_before_package",
    "sdk_after_package",
    "gate_submodule_import",
)
IMPORT_BEHAVIOR_ENVIRONMENT_REMOVALS = (
    "GATE_DISABLE",
    "SPENDGUARD_NO_AUTOINSTALL",
    "SPENDGUARD_REQUIRE",
    "OPENAI_API_KEY",
    "ANTHROPIC_API_KEY",
)
IMPORT_BEHAVIOR_NETWORK_PROXY = "http://127.0.0.1:9"
IMPORT_BEHAVIOR_NO_PROXY = "localhost,127.0.0.1"
IMPORT_BEHAVIOR_PROBE_ACTIONS = {
    "package_import": "import spendguard",
    "sdk_before_package": "import openai, anthropic\nimport spendguard",
    "sdk_after_package": "import spendguard\nimport openai, anthropic",
    "gate_submodule_import": "from spendguard.gate import install",
}


def load_import_behavior_snapshot(root):
    """Read the reviewed import-time golden data."""
    snapshot_path = (
        Path(root) / IMPORT_BEHAVIOR_PACKAGE_RELATIVE_ROOT / IMPORT_BEHAVIOR_SNAPSHOT_RESOURCE
    )
    return json.loads(snapshot_path.read_text())


def normalize_import_behavior_module_name(module_name, name_normalization):
    """Map a historical module name through every reviewed move."""
    visited = set()
    normalized = module_name
    while True:
        matching_names = [
            old_name
            for old_name in name_normalization
            if normalized == old_name or normalized.startswith(old_name + ".")
        ]
        if not matching_names:
            break
        old_name = max(matching_names, key=len)
        if normalized in visited:
            raise ValueError(f"Import snapshot name-normalization cycle at {normalized!r}")
        visited.add(normalized)
        normalized = name_normalization[old_name] + normalized[len(old_name):]
    return normalized


def normalize_import_behavior_entry(entry, name_normalization, executed_module_allowlist=()):
    """Normalize only module-bearing fields, preserving observed ordering."""
    normalized = dict(entry)
    normalized["executed_modules"] = [
        normalize_import_behavior_module_name(name, name_normalization)
        for name in entry["executed_modules"]
        if name not in executed_module_allowlist
    ]
    normalized["atexit_registrations"] = [
        normalize_import_behavior_module_name(name, name_normalization)
        for name in entry["atexit_registrations"]
    ]
    normalized["import_hooks"] = [
        normalize_import_behavior_module_name(name, name_normalization)
        for name in entry["import_hooks"]
    ]
    normalized["logging_handlers"] = [
        normalize_import_behavior_module_name(name, name_normalization)
        for name in entry["logging_handlers"]
    ]
    return normalized


def import_behavior_entries_match(
    expected, actual, name_normalization, executed_module_allowlist=(),
):
    """Compare complete entries after applying the reviewed move table to both."""
    return normalize_import_behavior_entry(
        expected, name_normalization, executed_module_allowlist,
    ) == normalize_import_behavior_entry(
        actual, name_normalization, executed_module_allowlist,
    )


def import_behavior_sdk_invariant_holds(entry):
    """Require every installed core SDK method to carry the gate wrapper marker."""
    return all(state in {"gated", "absent"} for state in entry["sdk_methods"].values())


def _import_behavior_bootstrap_source():
    return f'''import atexit
import importlib.machinery
import json
import logging
import os
import sys

_executed_modules = []
_spendguard_atexit_registrations = []
_original_register = atexit.register

def _recording_register(function, *arguments, **keywords):
    module_name = getattr(function, "__module__", "")
    if module_name == "spendguard" or module_name.startswith("{IMPORT_BEHAVIOR_MODULE_PREFIX}"):
        _spendguard_atexit_registrations.append(module_name + "." + getattr(function, "__qualname__", function.__class__.__qualname__))
    return _original_register(function, *arguments, **keywords)

atexit.register = _recording_register

class _ImportBehaviorLoader:
    def __init__(self, loader, fullname):
        self._loader = loader
        self._fullname = fullname

    def create_module(self, spec):
        create_module = getattr(self._loader, "create_module", None)
        return None if create_module is None else create_module(spec)

    def exec_module(self, module):
        _executed_modules.append(self._fullname)
        self._loader.exec_module(module)

    def __getattr__(self, name):
        return getattr(self._loader, name)

class _ImportBehaviorFinder:
    def find_spec(self, fullname, path=None, target=None):
        if not fullname.startswith("{IMPORT_BEHAVIOR_MODULE_PREFIX}"):
            return None
        spec = importlib.machinery.PathFinder.find_spec(fullname, path, target)
        if spec is not None and spec.loader is not None:
            spec.loader = _ImportBehaviorLoader(spec.loader, fullname)
        return spec

_import_behavior_finder = _ImportBehaviorFinder()
sys.meta_path.insert(0, _import_behavior_finder)

def _sdk_method_state(module_name, class_name):
    try:
        module = __import__(module_name, fromlist=[class_name])
        method = getattr(getattr(module, class_name), "create")
    except ModuleNotFoundError:
        return "absent"
    return "gated" if getattr(method, "_spend_gated", False) else "ungated"

def _spendguard_owned_name(value):
    owner = value.__class__.__module__ + "." + value.__class__.__qualname__
    return owner if owner.startswith("spendguard.") else None

def _emit_import_behavior_result():
    gate = sys.modules.get("spendguard.manage.admission.gate") or sys.modules.get("spendguard.gate")
    import_hooks = sorted(filter(None, (_spendguard_owned_name(finder) for finder in sys.meta_path)))
    logging_handlers = []
    loggers = [logging.getLogger()]
    loggers.extend(logger for logger in logging.Logger.manager.loggerDict.values() if isinstance(logger, logging.Logger))
    for logger in loggers:
        logging_handlers.extend(filter(None, (_spendguard_owned_name(handler) for handler in logger.handlers)))
    result = {{
        "executed_modules": _executed_modules,
        "sdk_methods": {{
            "anthropic.resources.messages.batches.Batches.create": _sdk_method_state("anthropic.resources.messages.batches", "Batches"),
            "openai.resources.files.Files.create": _sdk_method_state("openai.resources.files", "Files"),
        }},
        "gate_installed": bool(gate is not None and gate._enforcement_armed()),
        "gate_unarmed_details": [] if gate is None else gate._unarmed_sdk_details(),
        "atexit_registrations": _spendguard_atexit_registrations,
        "import_hooks": import_hooks,
        "logging_handlers": sorted(logging_handlers),
    }}
    print("{IMPORT_BEHAVIOR_RESULT_PREFIX}" + json.dumps(result, sort_keys=True, separators=(",", ":")))

_original_register(_emit_import_behavior_result)
'''


def _import_behavior_environment(root, probe_directory):
    environment = os.environ.copy()
    for name in IMPORT_BEHAVIOR_ENVIRONMENT_REMOVALS:
        environment.pop(name, None)
    source_root = Path(root) / "src"
    existing_python_path = environment.get("PYTHONPATH")
    python_paths = [os.fspath(probe_directory), os.fspath(source_root)]
    if existing_python_path:
        python_paths.append(existing_python_path)
    environment["PYTHONPATH"] = os.pathsep.join(python_paths)
    environment["SPENDGUARD_HOME"] = os.fspath(Path(probe_directory) / "spendguard-home")
    environment["http_proxy"] = environment["https_proxy"] = IMPORT_BEHAVIOR_NETWORK_PROXY
    environment["HTTP_PROXY"] = environment["HTTPS_PROXY"] = IMPORT_BEHAVIOR_NETWORK_PROXY
    environment["no_proxy"] = environment["NO_PROXY"] = IMPORT_BEHAVIOR_NO_PROXY
    return environment


def _write_import_behavior_startup_files(probe_directory, entry_point):
    from ...setup import _sitecustomize_hook_source

    bootstrap = _import_behavior_bootstrap_source()
    sitecustomize_path = Path(probe_directory) / "sitecustomize.py"
    if entry_point == "sitecustomize_hook":
        sitecustomize_path.write_text(bootstrap + "\n" + _sitecustomize_hook_source())
    else:
        sitecustomize_path.write_text(bootstrap)
    if entry_point == "usercustomize_hook":
        (Path(probe_directory) / "usercustomize.py").write_text(_sitecustomize_hook_source())
        sitecustomize_path.write_text(bootstrap + "\nimport usercustomize\n")


def capture_import_behavior_entry(root, entry_point, interpreter=None):
    """Capture one entry point in an isolated interpreter without invoking an SDK method."""
    if entry_point not in IMPORT_BEHAVIOR_ENTRY_POINTS:
        raise ValueError(f"Unknown import-behavior entry point: {entry_point}")
    interpreter = interpreter or sys.executable
    with tempfile.TemporaryDirectory(prefix=IMPORT_BEHAVIOR_PROBE_PREFIX) as probe_directory:
        _write_import_behavior_startup_files(probe_directory, entry_point)
        environment = _import_behavior_environment(root, probe_directory)
        if entry_point == "module_main":
            command = [interpreter, "-m", "spendguard", IMPORT_BEHAVIOR_OFFLINE_COMMAND]
        else:
            action = IMPORT_BEHAVIOR_PROBE_ACTIONS.get(entry_point, "")
            command = [interpreter, "-c", action]
        result = subprocess.run(
            command,
            cwd=root,
            env=environment,
            capture_output=True,
            text=True,
            timeout=IMPORT_BEHAVIOR_SUBPROCESS_TIMEOUT_SECONDS,
            check=False,
        )
    result_lines = [
        line.removeprefix(IMPORT_BEHAVIOR_RESULT_PREFIX)
        for line in result.stdout.splitlines()
        if line.startswith(IMPORT_BEHAVIOR_RESULT_PREFIX)
    ]
    if len(result_lines) != 1:
        raise RuntimeError(
            f"{entry_point} produced {len(result_lines)} import snapshots; "
            f"exit={result.returncode}, stderr={result.stderr!r}"
        )
    return json.loads(result_lines[0])


def capture_import_behavior_snapshot(root, interpreter=None):
    """Capture every reviewed entry point, each in its own fresh subprocess."""
    return {
        entry_point: capture_import_behavior_entry(root, entry_point, interpreter=interpreter)
        for entry_point in IMPORT_BEHAVIOR_ENTRY_POINTS
    }


def regenerate_import_behavior_snapshot(root, interpreter=None):
    """Explicitly replace the golden entries while preserving the reviewed move table."""
    root = Path(root).resolve()
    captured_entries = capture_import_behavior_snapshot(root, interpreter=interpreter)
    baseline_path = root / IMPORT_BEHAVIOR_PACKAGE_RELATIVE_ROOT / IMPORT_BEHAVIOR_SNAPSHOT_RESOURCE

    def replace_import_behavior_entries(data):
        data["schema_version"] = IMPORT_BEHAVIOR_SCHEMA_VERSION
        data.setdefault("name_normalization", {})
        data["entry_points"] = captured_entries

    config.update_json(
        baseline_path,
        replace_import_behavior_entries,
        reason="import behavior golden snapshot regeneration",
        keep_backups=0,
        required=True,
    )
    return captured_entries

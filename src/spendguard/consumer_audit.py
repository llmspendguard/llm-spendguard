"""Deterministic, subprocess-isolated audit of workspace LLM consumers."""
from __future__ import annotations

import argparse
import ast
import json
import os
from pathlib import Path
import subprocess

from . import adapters, config

CONSUMER_AUDIT_PROBE_TIMEOUT_SECONDS = 10.0
CONSUMER_AUDIT_GIT_TIMEOUT_SECONDS = 5.0
CONSUMER_AUDIT_VENV_PREFIX = ".venv"
CONSUMER_AUDIT_VENV_CONFIGURATION_NAME = "pyvenv.cfg"
# A realistic project subtree should fit within 4,096 visited directories. Exhausting
# this containment budget produces a fail-closed gap, so a conservative bound is safe.
CONSUMER_AUDIT_PRUNED_VENV_SCAN_BUDGET = 4096
CONSUMER_AUDIT_POSIX_EXECUTABLE_PATHS = (("bin", "python"), ("bin", "python3"))
CONSUMER_AUDIT_WINDOWS_EXECUTABLE_PATH = ("Scripts", "python.exe")
CONSUMER_AUDIT_SOURCE_SUFFIX = ".py"
CONSUMER_AUDIT_GIT_MARKER_NAME = ".git"
CONSUMER_AUDIT_BIN_DIRECTORY_NAME = "bin"
CONSUMER_AUDIT_PYTHON_EXECUTABLE_NAME = "python"
CONSUMER_AUDIT_GIT_EXECUTABLE = "git"
CONSUMER_AUDIT_GIT_HEAD_ARGUMENTS = ("rev-parse", "HEAD")
CONSUMER_AUDIT_GIT_ORIGIN_MAIN_ARGUMENTS = ("rev-parse", "origin/main")
CONSUMER_AUDIT_SPENDGUARD_IMPORT_NAME = adapters.SPENDGUARD_IMPORT_NAME


def _consumer_audit_sdk_import_names():
    return adapters.llm_capability_import_names() - {CONSUMER_AUDIT_SPENDGUARD_IMPORT_NAME}
CONSUMER_AUDIT_SUCCESS = "GATED+LATEST"
CONSUMER_AUDIT_NOT_ENFORCING = "NOT-ENFORCING"
CONSUMER_AUDIT_NO_SPENDGUARD = "NO-SPENDGUARD"
CONSUMER_AUDIT_BEHIND = "BEHIND"
CONSUMER_AUDIT_UNGATED_INTERPRETER = "UNGATED-INTERP"
CONSUMER_AUDIT_UNCONFIRMED_INTERPRETER = "INTERP-UNCONFIRMED"
CONSUMER_AUDIT_NOT_LLM = "NOT-LLM"
CONSUMER_AUDIT_EXEMPT = "EXEMPT"
CONSUMER_AUDIT_LLM_USE_UNDETERMINED = "LLM-USE-UNDETERMINED"
CONSUMER_AUDIT_ASSOCIATION_REPOSITORY = "repository"
CONSUMER_AUDIT_ASSOCIATION_UNASSOCIATED = "unassociated"
CONSUMER_AUDIT_ACTION_NONE = "none"
CONSUMER_AUDIT_ACTION_ARMED = "armed"
CONSUMER_AUDIT_ACTION_WOULD_ARM = "would-arm"
CONSUMER_AUDIT_PROBE = r'''import json, sys
result = {"spendguard_imports": False, "module_path": None, "version": None,
          "gate_preloaded": "spendguard" in sys.modules, "gate_enforcing": False,
          "gate_error": None, "sdk_imports": {}}
for name in ("openai", "anthropic"):
    try:
        __import__(name); result["sdk_imports"][name] = True
    except ImportError:
        result["sdk_imports"][name] = False
try:
    import spendguard
    result["spendguard_imports"] = True
    result["module_path"] = spendguard.__file__
    result["version"] = getattr(spendguard, "__version__", None)
    try:
        spendguard.require(); result["gate_enforcing"] = result["gate_preloaded"]
    except Exception as exc:
        result["gate_error"] = type(exc).__name__ + ": " + str(exc)
except ImportError as exc:
    result["gate_error"] = type(exc).__name__ + ": " + str(exc)
print(json.dumps(result, sort_keys=True))'''


def _path_is_within(candidate, parent):
    try:
        candidate.resolve().relative_to(parent.resolve())
        return True
    except ValueError:
        return False


def enumerate_consumer_repositories(roots, maximum_depth):
    """Return sorted git worktrees and the subtrees hidden by the configured depth bound."""
    repositories = set()
    pruned_at_depth = set()
    for root in sorted(Path(value).resolve() for value in roots):
        if not root.is_dir():
            continue
        for directory, names, _files in os.walk(root):
            current = Path(directory)
            depth = len(current.relative_to(root).parts)
            names[:] = sorted(name for name in names if not name.startswith(CONSUMER_AUDIT_VENV_PREFIX))
            if CONSUMER_AUDIT_GIT_MARKER_NAME in names or (current / CONSUMER_AUDIT_GIT_MARKER_NAME).is_file():
                repositories.add(current)
                names[:] = []
            elif depth >= maximum_depth:
                pruned_at_depth.update(current / name for name in names)
                names[:] = []
    return {
        "repositories": tuple(sorted(repositories, key=os.fspath)),
        "pruned_at_depth": tuple(sorted(pruned_at_depth, key=os.fspath)),
    }


def _is_venv_directory(path):
    if not path.is_dir():
        return False
    if (path / CONSUMER_AUDIT_VENV_CONFIGURATION_NAME).is_file():
        return True
    executable_paths = (*CONSUMER_AUDIT_POSIX_EXECUTABLE_PATHS, CONSUMER_AUDIT_WINDOWS_EXECUTABLE_PATH)
    return any(
        (candidate := path.joinpath(*relative_path)).is_file() and os.access(candidate, os.X_OK)
        for relative_path in executable_paths
    )


def _pruned_subtree_may_contain_venv(pruned_path):
    """Return whether a full bounded scan finds a venv or cannot prove absence."""
    if _is_venv_directory(pruned_path):
        return True
    visited_directory_count = 0
    traversal_errors = []
    for _directory, _names, files in os.walk(pruned_path, onerror=traversal_errors.append):
        visited_directory_count += 1
        if CONSUMER_AUDIT_VENV_CONFIGURATION_NAME in files:
            return True
        if visited_directory_count > CONSUMER_AUDIT_PRUNED_VENV_SCAN_BUDGET:
            return True
    return bool(traversal_errors)


def _consumer_path_is_exempt(repository, venv, exempt_paths):
    """Apply declared repo-or-venv path containment without semantic inference."""
    return any(
        (venv is not None and _path_is_within(venv, exempt_path))
        or (repository is not None and _path_is_within(repository, exempt_path))
        for exempt_path in exempt_paths
    )


def enumerate_consumer_venvs(repository, extra_venv_directories):
    """Return stable in-repository and configured external virtualenv candidates."""
    candidates = {child.resolve() for child in repository.iterdir() if child.is_dir() and _is_venv_directory(child)}
    for directory in sorted(extra_venv_directories, key=os.fspath):
        if directory.is_dir():
            candidates.update(
                child.resolve() for child in directory.iterdir()
                if child.is_dir() and _is_venv_directory(child)
            )
    return tuple(sorted(candidates, key=os.fspath))


def repository_import_facts(repository):
    """Parse Python imports outside virtualenvs; syntax, not semantic inference, defines LLM use."""
    sdk_import_names = _consumer_audit_sdk_import_names()
    source_text_tokens = tuple(sorted((*sdk_import_names, CONSUMER_AUDIT_SPENDGUARD_IMPORT_NAME)))
    imports = set()
    source_text_mentions = set()
    for source_path in sorted(repository.rglob(f"*{CONSUMER_AUDIT_SOURCE_SUFFIX}"), key=os.fspath):
        if any(
            _path_is_within(parent, repository) and _is_venv_directory(parent)
            for parent in source_path.parents
            if parent != repository
        ):
            continue
        try:
            source_text = source_path.read_text(errors="replace")
        except OSError:
            continue
        source_text_mentions.update(token for token in source_text_tokens if token in source_text)
        try:
            tree = ast.parse(source_text, filename=os.fspath(source_path))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imports.update(alias.name.partition(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imports.add(node.module.partition(".")[0])
    sdk_imports = tuple(sorted(imports & sdk_import_names))
    spendguard_import = CONSUMER_AUDIT_SPENDGUARD_IMPORT_NAME in imports
    imported_names = set(sdk_imports)
    if spendguard_import:
        imported_names.add(CONSUMER_AUDIT_SPENDGUARD_IMPORT_NAME)
    missing_import_mentions = tuple(sorted(source_text_mentions - imported_names))
    completeness_flag = (
        "text-mentions-not-imported:" + ",".join(missing_import_mentions)
        if missing_import_mentions else None
    )
    return {
        "sdk": sdk_imports,
        "spendguard_import": spendguard_import,
        "source_text_mentions": tuple(sorted(source_text_mentions)),
        "completeness_flag": completeness_flag,
    }


def probe_consumer_interpreter(python_executable, timeout=CONSUMER_AUDIT_PROBE_TIMEOUT_SECONDS):
    """Collect import and authoritative require() facts in the target interpreter."""
    try:
        completed = subprocess.run(
            [os.fspath(python_executable), "-c", CONSUMER_AUDIT_PROBE], capture_output=True,
            text=True, timeout=timeout, check=False,
        )
        if completed.returncode:
            return {"confirmed": False, "probe_error": (completed.stderr or completed.stdout).strip()}
        return {"confirmed": True, **json.loads(completed.stdout)}
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError) as exc:
        return {"confirmed": False, "probe_error": f"{type(exc).__name__}: {exc}"}


def classify_deployed_sha(checkout_head, origin_main_sha, deployed_sha):
    """Classify exact SHA facts without treating ordering as a heuristic."""
    if checkout_head == deployed_sha:
        return "=="
    if checkout_head == origin_main_sha and origin_main_sha != deployed_sha:
        return "behind"
    return "ahead-or-diverged"


def _git_output(checkout, *arguments):
    try:
        completed = subprocess.run(
            [CONSUMER_AUDIT_GIT_EXECUTABLE, "-C", os.fspath(checkout), *arguments], capture_output=True, text=True,
            timeout=CONSUMER_AUDIT_GIT_TIMEOUT_SECONDS, check=False,
        )
        return completed.stdout.strip() if completed.returncode == 0 else None
    except (OSError, subprocess.SubprocessError):
        return None


def _checkout_for_module(module_path):
    if not module_path:
        return None
    current = Path(module_path).resolve()
    for parent in current.parents:
        if (parent / CONSUMER_AUDIT_GIT_MARKER_NAME).exists():
            return parent
    return None


def _live_pids(python_executable):
    from . import fleet
    resolved = Path(python_executable).resolve()
    return sorted(
        row["pid"] for row in fleet.running_consumers(prune=False)
        if row.get("python_executable") and Path(row["python_executable"]).resolve() == resolved
    )


def _consumer_verdict(llm_calling, probe, deployed_sha):
    if llm_calling is not True:
        if llm_calling is False and probe.get("confirmed") and probe.get("gate_enforcing"):
            return CONSUMER_AUDIT_NOT_LLM
        return CONSUMER_AUDIT_LLM_USE_UNDETERMINED
    if not probe.get("confirmed"):
        return CONSUMER_AUDIT_UNCONFIRMED_INTERPRETER
    if not probe.get("spendguard_imports"):
        return CONSUMER_AUDIT_NO_SPENDGUARD
    if probe.get("sha_relation") is None:
        return CONSUMER_AUDIT_UNCONFIRMED_INTERPRETER
    if probe.get("sha_relation") != "==":
        return CONSUMER_AUDIT_BEHIND
    if not probe.get("gate_enforcing"):
        return CONSUMER_AUDIT_NOT_ENFORCING
    return CONSUMER_AUDIT_SUCCESS


def audit_consumer_workspaces(deployed_sha, roots=None, auto_arm=True, dry_run=False, settings=None):
    """Audit, narrowly arm safe venvs, re-probe, and return a deterministic proof record."""
    resolved_settings = settings or config.consumer_audit_settings()
    workspace_roots = tuple(Path(value).resolve() for value in (roots or resolved_settings["workspace_roots"]))
    extras = tuple(resolved_settings["workspace_extra_venv_dirs"])
    exempt_paths = tuple(Path(value).resolve() for value in resolved_settings["consumer_audit_exempt"])
    discovery = enumerate_consumer_repositories(workspace_roots, resolved_settings["workspace_scan_depth"])
    repositories = discovery["repositories"]
    pruned_at_depth = tuple(
        path for path in discovery["pruned_at_depth"]
        if not any(_path_is_within(path, exempt_path) for exempt_path in exempt_paths)
                and _pruned_subtree_may_contain_venv(path)
    )
    external_venv_names = {
        _normalized_consumer_name(child.name) for extra_root in extras if extra_root.is_dir()
        for child in extra_root.iterdir() if child.is_dir()
    }
    rows = []
    for repository in repositories:
        source_import_facts = repository_import_facts(repository)
        llm_repository = bool(source_import_facts["sdk"]) or source_import_facts["spendguard_import"]
        repository_venvs = tuple(
            venv for venv in enumerate_consumer_venvs(repository, ()) if _path_is_within(venv, repository)
        )
        if (not repository_venvs and llm_repository
                and _normalized_consumer_name(repository.name) not in external_venv_names):
            repository_is_exempt = _consumer_path_is_exempt(repository, None, exempt_paths)
            repository_verdict = (CONSUMER_AUDIT_EXEMPT if repository_is_exempt
                                  else CONSUMER_AUDIT_UNGATED_INTERPRETER)
            rows.append({"repo": os.fspath(repository), "venv": None, "python": None,
                         "sdk_imports_source": source_import_facts["sdk"],
                         "spendguard_import_source": source_import_facts["spendguard_import"],
                         "source_text_mentions": source_import_facts["source_text_mentions"],
                         "completeness_flag": source_import_facts["completeness_flag"],
                         "live_pids": [], "initial_verdict": repository_verdict,
                         "action": CONSUMER_AUDIT_ACTION_NONE, "final_verdict": repository_verdict,
                         "hint": (None if repository_is_exempt
                                  else "configure a gated virtualenv interpreter")})
        for venv in repository_venvs:
            rows.append(_audit_consumer_venv(repository, venv, source_import_facts, llm_repository, deployed_sha,
                                             auto_arm, dry_run,
                                             _consumer_path_is_exempt(repository, venv, exempt_paths)))
    for extra_root in sorted(extras, key=os.fspath):
        if not extra_root.is_dir():
            continue
        for venv in sorted((path for path in extra_root.iterdir() if path.is_dir()), key=os.fspath):
            if any(_path_is_within(venv, repository) for repository in repositories):
                continue
            normalized_venv_name = _normalized_consumer_name(venv.name)
            matching_repositories = tuple(
                repository for repository in repositories
                if _normalized_consumer_name(repository.name) == normalized_venv_name
            )
            associated_repository = matching_repositories[0] if len(matching_repositories) == 1 else None
            associated_import_facts = repository_import_facts(associated_repository) if associated_repository else {
                "sdk": (), "spendguard_import": False, "source_text_mentions": (), "completeness_flag": None,
            }
            repository_llm = (
                bool(associated_import_facts["sdk"]) or associated_import_facts["spendguard_import"]
                if associated_repository else None
            )
            rows.append(_audit_consumer_venv(associated_repository, venv, associated_import_facts, repository_llm,
                                             deployed_sha, auto_arm, dry_run,
                                             _consumer_path_is_exempt(associated_repository, venv, exempt_paths)))
    rows.sort(key=lambda row: ((row["repo"] or ""), (row["venv"] or "")))
    terminal_verdicts = (CONSUMER_AUDIT_SUCCESS, CONSUMER_AUDIT_NOT_LLM, CONSUMER_AUDIT_EXEMPT)
    remaining = sum(row["final_verdict"] not in terminal_verdicts for row in rows)
    remaining += len(pruned_at_depth)
    armed = sum(row["action"] == CONSUMER_AUDIT_ACTION_ARMED for row in rows)
    return {"deployed_sha": deployed_sha, "armed": armed, "remaining_gaps": remaining,
            "exit_code": 0 if remaining == 0 else 1, "dry_run": dry_run, "consumers": rows,
            "pruned_at_depth": tuple(os.fspath(path) for path in pruned_at_depth)}


def _normalized_consumer_name(name):
    """Normalize filesystem naming separators for an exact, non-semantic association fact."""
    return "".join(character.casefold() for character in name if character.isalnum())


def _audit_consumer_venv(repository, venv, source_import_facts, repository_llm, deployed_sha, auto_arm, dry_run,
                         consumer_is_exempt=False):
    python_executable = venv / CONSUMER_AUDIT_BIN_DIRECTORY_NAME / CONSUMER_AUDIT_PYTHON_EXECUTABLE_NAME
    probe = probe_consumer_interpreter(python_executable)
    checkout = _checkout_for_module(probe.get("module_path")) if probe.get("confirmed") else None
    head = _git_output(checkout, *CONSUMER_AUDIT_GIT_HEAD_ARGUMENTS) if checkout else None
    origin = _git_output(checkout, *CONSUMER_AUDIT_GIT_ORIGIN_MAIN_ARGUMENTS) if checkout else None
    probe["checkout"] = os.fspath(checkout) if checkout else None
    probe["checkout_head"] = head
    probe["origin_main"] = origin
    probe["sha_relation"] = classify_deployed_sha(head, origin, deployed_sha) if head else None
    llm_calling = repository_llm
    live_pids = _live_pids(python_executable) if python_executable.exists() else []
    initial = (CONSUMER_AUDIT_EXEMPT if consumer_is_exempt
               else _consumer_verdict(llm_calling, probe, deployed_sha))
    action = CONSUMER_AUDIT_ACTION_NONE
    final_probe = probe
    fixable = initial == CONSUMER_AUDIT_NOT_ENFORCING and not live_pids
    if fixable and auto_arm:
        if dry_run:
            action = CONSUMER_AUDIT_ACTION_WOULD_ARM
        else:
            from .setup import install_hook
            if install_hook(os.fspath(venv), install_pkg=False) == 0:
                action = CONSUMER_AUDIT_ACTION_ARMED
                final_probe = probe_consumer_interpreter(python_executable)
                final_probe.update({key: probe[key] for key in ("checkout", "checkout_head", "origin_main", "sha_relation")})
    final = (CONSUMER_AUDIT_EXEMPT if consumer_is_exempt
             else _consumer_verdict(llm_calling, final_probe, deployed_sha))
    hint = None
    if live_pids and initial != CONSUMER_AUDIT_SUCCESS:
        hint = "restart needed; live pids were not modified"
    elif initial == CONSUMER_AUDIT_NO_SPENDGUARD:
        hint = "install spendguard, then install-hook"
    elif initial == CONSUMER_AUDIT_BEHIND:
        hint = "update the spendguard checkout to the deployed sha"
    elif initial == CONSUMER_AUDIT_UNCONFIRMED_INTERPRETER:
        hint = "repair or confirm the interpreter"
    elif initial == CONSUMER_AUDIT_LLM_USE_UNDETERMINED:
        hint = "confirm whether this interpreter calls an LLM"
    return {"repo": os.fspath(repository) if repository else None, "venv": os.fspath(venv),
            "python": os.fspath(python_executable), "sdk_imports_source": source_import_facts["sdk"],
            "spendguard_import_source": source_import_facts["spendguard_import"],
            "source_text_mentions": source_import_facts["source_text_mentions"],
            "completeness_flag": source_import_facts["completeness_flag"], "probe": final_probe,
            "association": (CONSUMER_AUDIT_ASSOCIATION_REPOSITORY if repository
                            else CONSUMER_AUDIT_ASSOCIATION_UNASSOCIATED),
            "live_pids": live_pids, "initial_verdict": initial, "action": action,
            "final_verdict": final, "hint": hint}


def render_consumer_audit_table(result):
    lines = ["REPOSITORY | VENV | SDK IMPORTS | SPENDGUARD IMPORT | TEXT MENTIONS | COMPLETENESS | INITIAL | ACTION | FINAL | HINT"]
    for row in result["consumers"]:
        lines.append(" | ".join((row["repo"] or "-", row["venv"] or "-",
                                 ",".join(row["sdk_imports_source"]) or "-",
                                 str(row["spendguard_import_source"]),
                                 ",".join(row["source_text_mentions"]) or "-",
                                 row["completeness_flag"] or "-", row["initial_verdict"],
                                 row["action"], row["final_verdict"], row.get("hint") or "-")))
    lines.append(f"armed={result['armed']} remaining_gaps={result['remaining_gaps']}")
    lines.append("pruned_at_depth=" + json.dumps([os.fspath(path) for path in result["pruned_at_depth"]]))
    return "\n".join(lines)


def consumer_audit_command(argv=None, deployed_sha=None):
    parser = argparse.ArgumentParser(prog="spendguard consumer-audit")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--roots", nargs="+")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--no-auto-arm", action="store_true")
    arguments = parser.parse_args(argv)
    if deployed_sha is None:
        from .release import green_pointer
        deployed_sha = (green_pointer() or {}).get("sha")
    if not deployed_sha:
        parser.error("no deployed sha; run spendguard deploy first")
    result = audit_consumer_workspaces(deployed_sha, roots=arguments.roots,
                                       auto_arm=not arguments.no_auto_arm, dry_run=arguments.dry_run)
    print(json.dumps(result, indent=2, sort_keys=True) if arguments.json else render_consumer_audit_table(result))
    return result["exit_code"]

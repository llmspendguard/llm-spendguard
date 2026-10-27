"""Axis-4 guard: no Codex exec argv may combine agentic approval with an explicit sandbox."""
import ast
import tempfile
from pathlib import Path

from spendguard import codex_exec


SPENDGUARD_SOURCE_ROOT = Path(__file__).parents[1] / "src" / "spendguard"
AXIS4_NEGATIVE_SOURCE_FILENAME = "reintroduced_codex_flag_pair.py"
AXIS4_NAMED_CONSTANT_NEGATIVE_SOURCE_FILENAME = "reintroduced_named_codex_flag_pair.py"
AXIS4_EXEC_SUBCOMMAND_CONSTANT_NAME = "CODEX_EXEC_SUBCOMMAND"
AXIS4_APPROVAL_CONSTANT_NAME = "CODEX_AGENTIC_APPROVAL_FLAG"
AXIS4_SANDBOX_CONSTANT_NAME = "CODEX_SANDBOX_FLAG"
AXIS4_AGENTIC_CODEX_EXEC_SCOPE_NAME = "_run_agentic_codex_exec"


def codex_exec_argv_construction_sites(source_root):
    """Return source scopes that construct a Codex-exec argv."""
    construction_sites = []
    for source_path in sorted(source_root.rglob("*.py")):
        tree = ast.parse(source_path.read_text(encoding="utf-8"), filename=str(source_path))
        scopes = [node for node in ast.walk(tree)
                  if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))]
        for scope in scopes:
            values = {node.value for node in ast.walk(scope)
                      if isinstance(node, ast.Constant) and isinstance(node.value, str)}
            argv_names = {element.id for node in ast.walk(scope)
                          if isinstance(node, (ast.List, ast.Tuple))
                          for element in node.elts if isinstance(element, ast.Name)}
            constructs_exec = (codex_exec.CODEX_EXEC_SUBCOMMAND in values
                               or AXIS4_EXEC_SUBCOMMAND_CONSTANT_NAME in argv_names)
            if constructs_exec:
                construction_sites.append(
                    f"{source_path}:{getattr(scope, 'lineno', 1)}:{scope.name}"
                )
    return construction_sites


def codex_exec_approval_sandbox_pair_sites(source_root):
    """Return source scopes containing a constructed Codex-exec argv with the forbidden fixed flag pair."""
    violations = []
    for source_path in sorted(source_root.rglob("*.py")):
        tree = ast.parse(source_path.read_text(encoding="utf-8"), filename=str(source_path))
        scopes = [node for node in ast.walk(tree)
                  if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))]
        for scope in scopes:
            values = {node.value for node in ast.walk(scope)
                      if isinstance(node, ast.Constant) and isinstance(node.value, str)}
            names = {node.id for node in ast.walk(scope) if isinstance(node, ast.Name)}
            argv_names = {element.id for node in ast.walk(scope)
                          if isinstance(node, (ast.List, ast.Tuple))
                          for element in node.elts if isinstance(element, ast.Name)}
            constructs_exec = (codex_exec.CODEX_EXEC_SUBCOMMAND in values
                               or AXIS4_EXEC_SUBCOMMAND_CONSTANT_NAME in argv_names)
            uses_approval = (codex_exec.CODEX_AGENTIC_APPROVAL_FLAG in values
                             or AXIS4_APPROVAL_CONSTANT_NAME in names)
            uses_sandbox = (codex_exec.CODEX_SANDBOX_FLAG in values or AXIS4_SANDBOX_CONSTANT_NAME in names)
            if constructs_exec and uses_approval and uses_sandbox:
                scope_name = getattr(scope, "name", "<module>")
                violations.append(f"{source_path}:{getattr(scope, 'lineno', 1)}:{scope_name}")
    return violations


def test_repo_wide_codex_exec_argvs_keep_approval_and_sandbox_separate():
    assert codex_exec_approval_sandbox_pair_sites(SPENDGUARD_SOURCE_ROOT) == []


def test_repo_wide_codex_exec_guard_recognizes_real_construction_site():
    construction_sites = codex_exec_argv_construction_sites(SPENDGUARD_SOURCE_ROOT)
    assert any(site.endswith(f":{AXIS4_AGENTIC_CODEX_EXEC_SCOPE_NAME}")
               for site in construction_sites)


def test_repo_wide_codex_exec_guard_detects_reintroduced_pair(tmp_path):
    negative_source = tmp_path / AXIS4_NEGATIVE_SOURCE_FILENAME
    negative_source.write_text(
        "def construct_bad_command(executable):\n"
        f"    return [executable, {codex_exec.CODEX_EXEC_SUBCOMMAND!r}, "
        f"{AXIS4_APPROVAL_CONSTANT_NAME}, {codex_exec.CODEX_SANDBOX_FLAG!r}]\n",
        encoding="utf-8",
    )
    violations = codex_exec_approval_sandbox_pair_sites(tmp_path)
    assert len(violations) == 1
    assert AXIS4_NEGATIVE_SOURCE_FILENAME in violations[0]


def test_repo_wide_codex_exec_guard_detects_named_constant_reintroduced_pair(tmp_path):
    negative_source = tmp_path / AXIS4_NAMED_CONSTANT_NEGATIVE_SOURCE_FILENAME
    negative_source.write_text(
        "def construct_bad_named_constant_command(executable):\n"
        f"    return [executable, {AXIS4_EXEC_SUBCOMMAND_CONSTANT_NAME}, "
        f"{AXIS4_APPROVAL_CONSTANT_NAME}, {AXIS4_SANDBOX_CONSTANT_NAME}]\n",
        encoding="utf-8",
    )
    violations = codex_exec_approval_sandbox_pair_sites(tmp_path)
    assert len(violations) == 1
    assert AXIS4_NAMED_CONSTANT_NEGATIVE_SOURCE_FILENAME in violations[0]


def test_runtime_codex_exec_boundary_rejects_forbidden_pair():
    command = [codex_exec.CODEX_EXEC_SUBCOMMAND, codex_exec.CODEX_AGENTIC_APPROVAL_FLAG,
               codex_exec.CODEX_SANDBOX_FLAG]
    try:
        codex_exec._validate_codex_exec_approval_boundary(command)
    except ValueError as exc:
        assert codex_exec.CODEX_AGENTIC_APPROVAL_FLAG in str(exc)
        assert codex_exec.CODEX_SANDBOX_FLAG in str(exc)
    else:
        raise AssertionError("forbidden Codex exec approval/sandbox pair was accepted")


if __name__ == "__main__":
    test_repo_wide_codex_exec_argvs_keep_approval_and_sandbox_separate()
    test_repo_wide_codex_exec_guard_recognizes_real_construction_site()
    with tempfile.TemporaryDirectory() as axis4_negative_directory:
        test_repo_wide_codex_exec_guard_detects_reintroduced_pair(Path(axis4_negative_directory))
    with tempfile.TemporaryDirectory() as axis4_named_negative_directory:
        test_repo_wide_codex_exec_guard_detects_named_constant_reintroduced_pair(
            Path(axis4_named_negative_directory)
        )
    test_runtime_codex_exec_boundary_rejects_forbidden_pair()
    print("OK test_codex_exec_approval_sandbox_axis4")

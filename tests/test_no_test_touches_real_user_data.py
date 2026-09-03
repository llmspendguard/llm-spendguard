"""No test may write to a real user path. Enforced, because prose did not hold.

WHAT HAPPENED. A guard added on 2026-08-10 to prove the config cache re-reads a changed file did this:

    _cfg.CONFIG_JSON.write_text(json.dumps({"probe": {"k": "first"}}))

`_cfg.CONFIG_JSON` is ~/.spendguard/config.json, and the test that ran it shares the real HOME. It
OVERWROTE the user's settings — the file went from ~9KB to 26 bytes — and every knob in it was lost.
`calls.enabled` silently became False, so the next review wave recorded nothing to the call log, and that
wave's independent ledger cross-check read $0.00. A guard that damages the thing it guards is worse than
no guard, and this one was invisible: the suite stayed green throughout.

WORSE: there was no backup. ~/.spendguard holds the spend ledger too — 43,488 rows and $24,515 of
recorded spend at the time — and the machine's B2 job mirrors ~/.claude only. The standing rule here is
integrity before any mutation; a mutation with no recovery path is not a change, it is a gamble.

WHAT THIS ENFORCES. Every test script either:
  * re-execs itself into a temp SPENDGUARD_HOME before importing anything (the pattern most already use), or
  * appears in READ_ONLY below, meaning it was read and confirmed to write nothing outside a temp dir.

The list is deliberately explicit. A new test that touches real state has to be argued for in this file
rather than discovered later by the damage.
"""
import ast
import pathlib
import sys

HERE = pathlib.Path(__file__).parent

# Isolation is a STRUCTURAL fact of the code, read with ast — not a substring/position guess. An earlier
# substring version was fooled by a COMMENTED-OUT `# os.environ["SPENDGUARD_HOME"] = home` sitting before the
# import (a comment isn't code, but it IS in the text); ast never sees comments or string literals, so it can't
# be. The sibling guard (test_no_call_can_be_silently_truncated) walks ast for exactly this reason.


def _is_home_target(node):
    """AST node is a real `os.environ["SPENDGUARD_HOME"]` subscript target (either quote style)."""
    if not isinstance(node, ast.Subscript):
        return False
    v = node.value
    if not (isinstance(v, ast.Attribute) and v.attr == "environ"
            and isinstance(v.value, ast.Name) and v.value.id == "os"):
        return False
    key = node.slice.value if isinstance(node.slice, ast.Constant) else getattr(node.slice, "value", None)
    return key == "SPENDGUARD_HOME"


def _is_mkdtemp_call(node):
    """AST node is a real `tempfile.mkdtemp(...)` (or bare `mkdtemp(...)`) call."""
    return isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "mkdtemp"


def _isolation_facts(src):
    """(import_line, tempdir_home_lines) from the AST, proving the SEMANTIC link — not just co-occurrence. A
    `tempdir_home_line` is the line of an `os.environ["SPENDGUARD_HOME"] = <v>` whose VALUE is a throwaway temp
    dir: either `<v>` is a tempfile.mkdtemp() call directly (inline form), or `<v>` is a NAME that was assigned
    from a mkdtemp() call earlier (two-step form). A HOME set to a real string path, or to a variable never bound
    to mkdtemp, does NOT count — so `unused = mkdtemp(); os.environ["SPENDGUARD_HOME"] = "/real"` is correctly
    NOT isolated. This is a bounded data-flow FACT (deterministic), not a judgement about intent."""
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return (None, [])
    import_line, tempdir_vars, home_assigns = None, {}, []
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            mod = node.module if isinstance(node, ast.ImportFrom) else None
            names = [n.name for n in node.names]
            if (mod and mod.split(".")[0] == "spendguard") or any(n.split(".")[0] == "spendguard" for n in names):
                if import_line is None or node.lineno < import_line:
                    import_line = node.lineno
        if isinstance(node, ast.Assign):
            if _is_mkdtemp_call(node.value):                    # X = tempfile.mkdtemp(...)  → X names a temp dir
                for t in node.targets:
                    if isinstance(t, ast.Name):
                        tempdir_vars.setdefault(t.id, node.lineno)
            for t in node.targets:                              # os.environ["SPENDGUARD_HOME"] = <value>
                if _is_home_target(t):
                    home_assigns.append((node.lineno, node.value))
    tempdir_home_lines = []
    for lineno, val in home_assigns:
        bound_to_temp = _is_mkdtemp_call(val) or (
            isinstance(val, ast.Name) and val.id in tempdir_vars and tempdir_vars[val.id] < lineno)
        if bound_to_temp:
            tempdir_home_lines.append(lineno)
    return (import_line, tempdir_home_lines)


def _isolated_before_import(src):
    """True if the test redirects SPENDGUARD_HOME to a value PROVABLY bound to a tempfile.mkdtemp dir BEFORE
    importing the package — so it cannot resolve a real user path. Structural + bounded data-flow (ast), so it
    recognizes both the inline and two-step forms, is immune to comments, and rejects a HOME set to a real path
    even when an unrelated mkdtemp() sits nearby."""
    import_line, tempdir_home_lines = _isolation_facts(src)
    if not tempdir_home_lines:
        return False
    if import_line is None:                    # no package import at all → nothing resolves a real path anyway
        return True
    return min(tempdir_home_lines) < import_line

# Tests confirmed by reading to write nothing outside a temp directory: pure functions, source inspection,
# in-memory fixtures, or their own tempfile.mkdtemp. Adding a name here is a claim you have checked it.
READ_ONLY = {
    "test_runner.py", "test_no_test_touches_real_user_data.py",
}

failures = 0


def check(label, cond, extra=""):
    global failures
    if not cond:
        failures += 1
    print(f"  [{'OK' if cond else 'FAIL'}] {label}{('  — ' + extra) if extra and not cond else ''}")


# The writes that reach real user state. These are attribute/callable references, not arbitrary paths:
# a test naming one of them without isolation is writing where the user's data lives.
REAL_PATHS = ("CONFIG_JSON", "saas_path()", "user_prices_path()", "db_path()", "config.HOME")
WRITES = (".write_text(", ".write_bytes(", "open(", "os.remove(", "os.replace(", "shutil.")

unisolated = []
for f in sorted(HERE.glob("test_*.py")):
    if f.name in READ_ONLY:
        continue
    src = f.read_text()
    if _isolated_before_import(src):
        continue                       # redirects HOME to a temp dir before import: cannot reach real state
    hits = [p for p in REAL_PATHS if p in src and any(w in src for w in WRITES)]
    if hits:
        unisolated.append((f.name, hits))

check("no test writes to a real user path without isolating SPENDGUARD_HOME first",
      not unisolated,
      "; ".join(f"{n} references {h}" for n, h in unisolated)
      + " — re-exec into a tempfile.mkdtemp SPENDGUARD_HOME, or redirect the path for the duration")

# And the redirect must come BEFORE the package import, or the module has already resolved its paths from the real
# home and the redirect is decorative. A test that sets a temp HOME but only AFTER importing spendguard is flagged.
# Read from the AST (real assignment/import line numbers), so a comment or a string can't move the verdict.
late = []
for f in sorted(HERE.glob("test_*.py")):
    import_line, tempdir_home_lines = _isolation_facts(f.read_text())
    if not tempdir_home_lines or import_line is None:
        continue                           # no temp-home redirect + import pair to order — not this check's concern
    if min(tempdir_home_lines) > import_line:   # every tempdir HOME redirect lands AFTER the import → decorative
        late.append(f.name)
check("isolation happens BEFORE spendguard is imported",
      not late, ", ".join(late) + " — the package resolves its paths at import time")

print(f"\n{'[FAIL]' if failures else 'OK'} test_no_test_touches_real_user_data: {failures} failure(s)")
sys.exit(1 if failures else 0)

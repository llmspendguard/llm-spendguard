"""Compacting source for review must never change what the reviewer is looking at.

WHY THIS GUARD EXISTS. Shrinking a file to fit a vendor's payload ceiling is only worth doing if the CODE
survives byte-for-byte — a reviewer hunting correctness defects in source we damaged is worse than no review,
and worse still because the damage looks like a finding. Two real defects were caught by the module's own
re-parse check during development, both from rebuilding source instead of editing it:

  * a token-position rebuild silently dropped BACKSLASH line-continuations (tokenize consumes a trailing `\\`
    as whitespace and emits no token for it), so `x = \\` + newline came back as two statements and five
    modules stopped parsing;
  * collapsing blank lines removed blank lines INSIDE triple-quoted strings, changing the value.

Both are the same mistake: reproducing a lexer's output means reproducing every lexical rule you did not
think about. Editing spans in place reproduces none of them.
"""
import ast
import pathlib
import sys

from spendguard.source_compact import compact

REPO = pathlib.Path(__file__).resolve().parents[1] / "src" / "spendguard"
failures = 0


def check(label, ok, extra=""):
    global failures
    if not ok:
        failures += 1
    print(f"  [{'OK' if ok else 'FAIL'}] {label}" + (f"  — {extra}" if not ok and extra else ""))


def test_every_shipped_module_survives():
    """The whole package is the corpus. A compressor tested on toy input is tested on nothing."""
    bad, shrank = [], []
    for f in sorted(REPO.glob("*.py")):
        src = f.read_text()
        out, st = compact(src)
        if not st["ok"] and len(src) > 200:
            bad.append(f.name)
            continue
        try:
            ast.parse(out)
        except SyntaxError as e:
            bad.append(f"{f.name}:{e.lineno}")
        if st["ok"] and st["saved_pct"] > 0:
            shrank.append(st["saved_pct"])
    check(f"every module compacts and still parses ({len(shrank)} shrank)", not bad, str(bad[:4]))


def test_code_statements_are_preserved_exactly():
    """Docstrings and comments go; every other statement stays, and stays in the same order. Compared as
    ASTs with docstrings removed from both sides, so this cannot pass by both being equally broken."""
    def skeleton(src):
        """Both sides normalised the same way: a leading docstring OR the `...` that replaces one is dropped
        entirely. Replacing a docstring with a placeholder on one side only compares a tree that has an extra
        node against one that does not — which is what the first version of this test did, and it reported a
        defect in the compactor that was really a defect in the comparison."""
        tree = ast.parse(src)
        for node in ast.walk(tree):
            body = getattr(node, "body", None)
            if not isinstance(body, list) or not body:
                continue
            first = body[0]
            is_doc = (isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant)
                      and isinstance(first.value.value, str))
            is_ell = (isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant)
                      and first.value.value is Ellipsis)
            if (is_doc or is_ell) and len(body) > 1:
                del body[0]
            elif is_doc or is_ell:
                body[0] = ast.Pass()          # an empty block is invalid; both sides get the same filler
        return ast.dump(ast.parse(ast.unparse(tree)))

    mismatched = []
    for f in sorted(REPO.glob("*.py"))[:25]:
        src = f.read_text()
        out, st = compact(src)
        if not st["ok"]:
            continue
        if skeleton(src) != skeleton(out):
            mismatched.append(f.name)
    check("code is preserved exactly — only prose is removed", not mismatched, str(mismatched[:4]))


def test_line_numbers_are_preserved_exactly():
    """The claim that cost a $6.25 review. This docstring asserted "line numbers in a finding still point at
    real code" while the code DELETED lines — shifting everything below by up to 243 and pointing all 206
    findings at the wrong place. The findings were correct; nobody could act on them. A blank line costs one
    byte. Assert the property, not the intention."""
    off = []
    for f in sorted(REPO.glob("*.py")):
        src = f.read_text()
        out, st = compact(src)
        if not st["ok"]:
            continue
        if len(out.splitlines()) != len(src.splitlines()):
            off.append(f"{f.name}({len(src.splitlines())}->{len(out.splitlines())})")
    check("compacting never renumbers a file", not off, str(off[:4]))


def test_every_kept_line_is_byte_identical_at_the_same_index():
    """Stronger than a line count. A non-blank output line must be a PREFIX of the input line at the SAME
    index — which permits a trailing comment to be trimmed (the point of the exercise) while proving nothing
    was moved, reordered, or rewritten. Asserting byte-equality instead would fail on every `code  # note`
    line and prove only that the assertion was wrong."""
    bad = []
    for f in sorted(REPO.glob("*.py"))[:30]:
        src = f.read_text()
        out, st = compact(src)
        if not st["ok"]:
            continue
        for i, (o, c) in enumerate(zip(src.splitlines(), out.splitlines()), 1):
            # `...` is the ONE documented substitution: a body whose only statement was a docstring would
            # otherwise be an empty block, which does not parse. Everything else must be a prefix.
            if c.strip() == "...":
                continue
            if c.strip() and not o.rstrip().startswith(c.rstrip()):
                bad.append(f"{f.name}:{i} {c.strip()[:40]!r} vs {o.strip()[:40]!r}")
                break
    check("a non-blank output line is the input line at the same index (comment trimmed at most)",
          not bad, str(bad[:3]))


def test_a_backslash_continuation_survives():
    """The exact defect that broke five modules."""
    src = 'x = 1\ny = \\\n    x + 1\n'
    out, st = compact(src)
    check("a backslash continuation is not turned into two statements", st["ok"] and "y" in out, repr(out))
    ast.parse(out)


def test_a_blank_line_inside_a_string_survives():
    src = 'S = """a\n\nb"""\ndef f():\n    return S\n'
    out, st = compact(src)
    check("a blank line inside a string literal is preserved", "a\n\nb" in out, repr(out))


def test_a_stub_whose_only_statement_is_a_docstring_stays_valid():
    src = 'def f():\n    """just docs"""\n'
    out, st = compact(src)
    check("a docstring-only body becomes `...`, not an empty block", st["ok"] and "..." in out, repr(out))
    ast.parse(out)


def test_unparseable_input_is_returned_untouched_and_says_so():
    src = "def broken(:\n"
    out, st = compact(src)
    check("broken input is returned unchanged", out == src)
    check("...and ok=False says the pass-through happened", st["ok"] is False)


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print(f"\n{'[FAIL]' if failures else 'OK'} test_source_compact: {failures} failure(s)")
    sys.exit(1 if failures else 0)

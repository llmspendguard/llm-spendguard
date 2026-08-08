"""Shrink a source file for review WITHOUT splitting it — keep every line of code, drop the prose.

WHY THIS EXISTS. Chunking a file to fit a vendor's payload ceiling costs the reviewer the thing that makes a
review good: seeing the whole unit. A function reviewed in isolation cannot be judged against the invariant
declared 200 lines above it. Compression is the alternative that keeps the file whole — docstrings and
comments are the largest removable mass in a heavily-documented codebase, and a reviewer looking for
correctness defects reads the CODE, not the narration around it.

WHAT IT KEEPS, and why the distinction matters
  Every statement, signature, decorator, default and control-flow line is preserved EXACTLY, so line numbers
  in a finding still point at real code. Only docstring expressions and comment tokens are removed, and a
  docstring is replaced by `...` rather than deleted, because deleting the only statement in a stub function
  changes the file from valid to unparseable — a "compression" that alters semantics is not compression.

WHAT IT COSTS. Docstrings sometimes carry the INTENT that makes a defect visible ("callers must hold the
lock", "sizes are in BYTES"). Stripping them can hide a bug that only reads as a bug against the stated
contract. That is a real trade, not a free win, so `keep_module_doc` retains the module-level docstring by
default: it is usually where the invariants live, and it is one string per file rather than one per function.

VERIFY, DO NOT ASSUME. compact() re-parses its own output and returns the original unchanged if the result
does not parse. A compressor that silently emits broken source would send a reviewer looking for bugs in
damage we caused.
"""
import ast
import io
import tokenize


def _docstring_line_spans(tree):
    """Line spans of every docstring EXPRESSION in the tree. AST, not pattern-matching: a string literal that
    happens to sit at the top of a function is a docstring only by position, which the parser already knows."""
    spans = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        body = getattr(node, "body", None) or []
        if not body:
            continue
        first = body[0]
        if (isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant)
                and isinstance(first.value.value, str)):
            spans.append((first.lineno, first.end_lineno, isinstance(node, ast.Module), len(body) == 1))
    return spans


def _string_lines(src):
    """Every physical line that lies inside a STRING token. A blank line in there is part of the value."""
    inside = set()
    try:
        for tok in tokenize.generate_tokens(io.StringIO(src).readline):
            if tok.type == tokenize.STRING and tok.end[0] > tok.start[0]:
                inside.update(range(tok.start[0], tok.end[0] + 1))
    except (tokenize.TokenError, IndentationError, SyntaxError):
        pass
    return inside


def _strip_comments(src):
    """Blank out COMMENT spans IN PLACE, leaving every other byte where it was.

    The first version rebuilt the file from tokens using their row/column positions, and silently lost
    BACKSLASH line-continuations — tokenize consumes a trailing `\\` as whitespace and emits no token for it,
    so `x = \\` + newline came back as two statements and five modules stopped parsing. Rebuilding source from
    a lexer means reproducing every lexical rule you did not think about; editing spans means reproducing
    none of them. tokenize is used only to LOCATE comments, which is what it is reliable for."""
    try:
        toks = list(tokenize.generate_tokens(io.StringIO(src).readline))
    except (tokenize.TokenError, IndentationError, SyntaxError):
        return src
    lines = src.splitlines(keepends=True)
    for tok in toks:
        if tok.type != tokenize.COMMENT:
            continue
        row, col = tok.start
        if row - 1 >= len(lines):
            continue
        line = lines[row - 1]
        head = line[:col]
        # A comment on its own line leaves an empty line; one after code leaves the code plus a newline.
        lines[row - 1] = (head.rstrip() + "\n") if head.strip() else "\n"
    return "".join(lines)


def compact(src, *, keep_module_doc=True, strip_comments=True):
    """(compacted_source, stats). Never raises, and never returns source that does not parse.

    stats: {orig_chars, out_chars, saved_pct, docstrings_removed, ok} — `ok` False means the input could not
    be parsed and was returned untouched, which is a fact the caller must be able to see rather than a silent
    pass-through."""
    stats = {"orig_chars": len(src), "out_chars": len(src), "saved_pct": 0.0,
             "docstrings_removed": 0, "ok": False}
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return src, stats
    lines = src.splitlines(keepends=True)
    drop = set()
    removed = 0
    for start, end, is_module, sole in _docstring_line_spans(tree):
        if is_module and keep_module_doc:
            continue
        for ln in range(start, (end or start) + 1):
            drop.add(ln)
        removed += 1
        if sole:
            # The docstring was the ONLY statement. Removing it outright makes the block empty and the file
            # unparseable, so the body becomes `...` — same shape, no narration.
            indent = len(lines[start - 1]) - len(lines[start - 1].lstrip())
            lines[start - 1] = " " * indent + "...\n"
            drop.discard(start)
    kept = [ln for i, ln in enumerate(lines, 1) if i not in drop]
    out = "".join(kept)
    if strip_comments:
        out = _strip_comments(out)
    # Blank lines are dropped ONLY where they are not inside a string literal. A blank line within a
    # triple-quoted block is part of the value, and removing it changes the string — which is how the first
    # version left five modules unparseable. The set comes from the lexer, not from looking at the text.
    protected = _string_lines(out)
    out = "".join(ln for i, ln in enumerate(out.splitlines(keepends=True), 1)
                  if ln.strip() or i in protected)
    out = out if out.endswith("\n") else out + "\n"
    try:
        ast.parse(out)                      # VERIFY. A compressor that emits broken source is worse than none.
    except SyntaxError:
        return src, stats
    stats.update(out_chars=len(out), docstrings_removed=removed, ok=True,
                 saved_pct=round((1 - len(out) / len(src)) * 100, 1) if src else 0.0)
    return out, stats

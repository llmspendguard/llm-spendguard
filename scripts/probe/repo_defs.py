"""SCOPE-QUALIFIED definition inventory — the one place the review axes learn what a function IS.

WHY SCOPE. The first version of the capability audit keyed definitions on `module.name`, which COLLIDED 11
times in this repo: `gate._StreamProxy.__init__` and `gate._AsyncStreamProxy.__init__` both became
`gate.__init__`, so the slice reviewer read one body and attributed it to the other. A review that reads the
wrong code is worse than no review — it produces confident findings about text that isn't there.

WHY NAMES ARE NOT THE CONCEPT. That same audit clustered from NAME + SIGNATURE + DOCSTRING. Those are the
author's CLAIM about a function, not its behaviour, and this repo is full of counter-examples in both
directions:
  · `bulkgate.record_estimate` and `calibrate.record_estimate` share a name and do DIFFERENT jobs
    (one writes gate_ledger to authorize spend, the other writes cost_predictions to train the estimator)
  · `share._scrub_text` and `share.scrub` share almost nothing and do the SAME job
So `concept` must be judged from the BODY. Names are used here only for IDENTITY — which definition site is
which — never as evidence of what it does.

WHY POLYMORPHISM IS NOT DUPLICATION. `truth_total` appears five times, in five classes, because five
providers implement one `Source` protocol. That is the design working. `_state_path` appears four times as
four copies of the same three lines. Both are "same name, different files"; only one is a defect. Telling
them apart is a judgement about intent, so it goes to a model (see review_axes / capability_audit), never to
a rule about names.

  defs(root)        → [Definition]                scope-qualified, one entry per definition site
  by_bare_name()    → {bare: [Definition]}        for the repo-wide name-uniqueness question
"""
import ast, collections, pathlib


class Definition:
    """One definition site. `qual` is unique across the repo BY CONSTRUCTION (module + class chain + name)."""

    __slots__ = ("module", "qual", "bare", "scope", "lineno", "src", "sig", "doc", "is_method")

    def __init__(self, module, scope, node, lines):
        self.module = module
        self.scope = list(scope)
        self.bare = node.name
        self.qual = ".".join([module] + self.scope + [node.name])
        self.lineno = node.lineno
        self.src = "\n".join(lines[node.lineno - 1:(node.end_lineno or node.lineno)])
        self.sig = "(" + ", ".join(a.arg for a in node.args.args) + ")"
        d = (ast.get_docstring(node) or "").strip().splitlines()
        self.doc = d[0] if d else ""
        self.is_method = bool(scope)

    def __repr__(self):
        return f"<{self.qual}>"


def defs(root, pattern="*.py"):
    """Every definition under `root`, scope-qualified. Nested defs and methods each get their own entry."""
    out = []
    for f in sorted(pathlib.Path(root).rglob(pattern)):
        try:
            txt = f.read_text(errors="ignore")
            tree = ast.parse(txt)
        except (SyntaxError, OSError, ValueError):
            # A FILE WE CANNOT PARSE IS UNREVIEWED, NOT CLEAN. Callers that report coverage must count these.
            out.append(None)
            continue
        lines = txt.splitlines()

        def walk(node, scope):
            for ch in ast.iter_child_nodes(node):
                if isinstance(ch, ast.ClassDef):
                    walk(ch, scope + [ch.name])
                elif isinstance(ch, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    out.append(Definition(f.stem, scope, ch, lines))
                    walk(ch, scope + [ch.name])
        walk(tree, [])
    return [d for d in out if d is not None]


def unparsed(root, pattern="*.py"):
    """Files that could not be parsed — they are UNREVIEWED by every axis, and must be reported as such."""
    bad = []
    for f in sorted(pathlib.Path(root).rglob(pattern)):
        try:
            ast.parse(f.read_text(errors="ignore"))
        except (SyntaxError, OSError, ValueError) as e:
            bad.append((str(f), type(e).__name__))
    return bad


def by_bare_name(ds):
    m = collections.defaultdict(list)
    for d in ds:
        m[d.bare].append(d)
    return m

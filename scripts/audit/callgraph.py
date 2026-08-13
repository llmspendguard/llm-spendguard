"""AST call graph across the package — who calls whom, function to function, across files.

PARSING, not judgement: it reads def/Call nodes out of the syntax tree. Cross-file resolution is by
imported name (module.fn and bare fn resolved against each file's imports), which is exact for this repo's
`from . import x` / `from .x import y` style. Emits an adjacency map and answers who-calls / calls-what.
"""
import ast, pathlib, sys, json, collections

SRC = pathlib.Path("src/spendguard")

def module_defs(tree):
    out = set()
    for n in ast.walk(tree):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
            out.add(n.name)
    return out

def build():
    # name -> module for every top-level def, to resolve bare calls
    owner, imports, edges = {}, {}, collections.defaultdict(set)
    trees = {}
    for p in sorted(SRC.glob("*.py")):
        try: t = ast.parse(p.read_text(errors="ignore"))
        except SyntaxError: continue
        trees[p.stem] = t
        for d in module_defs(t):
            owner.setdefault(d, p.stem)
    for mod, t in trees.items():
        # imported module aliases: `from . import budget` -> budget; `from .x import y` -> y@x
        imp = {}
        for n in ast.walk(t):
            if isinstance(n, ast.ImportFrom) and (n.module or n.level):
                for a in n.names:
                    imp[a.asname or a.name] = a.name
        # walk each function, collect calls
        cur = [None]
        class V(ast.NodeVisitor):
            def visit_FunctionDef(self, node):
                prev = cur[0]; cur[0] = f"{mod}.{node.name}"; self.generic_visit(node); cur[0] = prev
            visit_AsyncFunctionDef = visit_FunctionDef
            def visit_Call(self, node):
                f = node.func
                callee = None
                if isinstance(f, ast.Attribute):
                    base = f.value.id if isinstance(f.value, ast.Name) else None
                    if base in imp:              # module.fn  (e.g. budget.record)
                        callee = f"{base}.{f.attr}"
                    elif base == "self":
                        callee = f"{mod}.{f.attr}"
                elif isinstance(f, ast.Name):
                    if f.id in owner:            # bare fn defined somewhere in the pkg
                        callee = f"{owner[f.id]}.{f.id}"
                if callee and cur[0]:
                    edges[cur[0]].add(callee)
                self.generic_visit(node)
        V().visit(t)
    return owner, edges

def main():
    owner, edges = build()
    cmd = sys.argv[1] if len(sys.argv) > 1 else "stats"
    if cmd == "callers":                          # who calls X
        target = sys.argv[2]
        for src, outs in sorted(edges.items()):
            for o in outs:
                if o == target or o.endswith("." + target):
                    print(f"  {src}  ->  {o}")
    elif cmd == "calls":                          # what X calls
        for o in sorted(edges.get(sys.argv[2], [])):
            print(f"  {sys.argv[2]}  ->  {o}")
    elif cmd == "chain":                           # BFS reachable from X
        seen, q = set(), [sys.argv[2]]
        while q:
            n = q.pop(0)
            for o in sorted(edges.get(n, [])):
                if o not in seen:
                    seen.add(o); print(f"  {n} -> {o}"); q.append(o)
    else:
        print(f"  functions: {len(owner)}  ·  call edges: {sum(len(v) for v in edges.values())}")
        print(f"  modules: {len(set(owner.values()))}")
        # most-called
        indeg = collections.Counter(o for outs in edges.values() for o in outs)
        print("  most-called functions:")
        for name, n in indeg.most_common(12):
            print(f"    {n:>4}  {name}")

if __name__ == "__main__":
    main()

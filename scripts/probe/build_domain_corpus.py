#!/usr/bin/env python3
"""Build exhaustive static evidence for spendguard domain discovery."""

from __future__ import annotations

import argparse
import ast
from pathlib import Path

DISCOVERY_INSTRUCTION = """> You are an expert software architect doing a DDD bounded-context analysis of the Python package `llm-spendguard`
> (a multi-currency "token bank" / LLM-cost-governance engine). Below is the FULL evidence corpus: every module with
> its purpose and internal import edges, three known failure signals from a prior attempt, and a candidate "seed"
> taxonomy. Propose the domain structure that best organizes ALL modules. Organize PRIMARILY by capability kind —
> GENERATE (produces spend/data), ACTIVELY-MANAGE (live resource/money management: routing, queue, delegation,
> warm-daemon pools), STORE (systems of record), ANALYZE (measurement, valuation, forensic reconciliation),
> INTERFACE (thin facades cli/mcp/api that own NO capability) — and secondarily by dependency layer.
> HARD RULES: (1) every module lands in EXACTLY ONE domain with a clean fit; (2) if a module has no clean home that
> means a MISSING DOMAIN — name it; NEVER smear a module into a loosely-related bucket and call the result "healthy
> distribution"; (3) a module with >1 responsibility is SPLIT across its true domains and you state which
> responsibility goes where; (4) interface surfaces own no capability; (5) keep the economic loop acyclic
> (ledger→measurement→learning→valuation→routing→execution→ledger). Treat the seed as a hypothesis to confirm or
> refute, not an answer to copy. Output STRICT JSON only:
> {"domains":[{"name","capability_kind","layer","responsibility","modules":[...]}],
>  "missing_domains_found":[...],"split_modules":[{"module","responsibilities":[{"domain","what"}]}],
>  "homeless":[...],"notes":"..."}.
> The union of all domains[].modules plus split_modules must equal the full module list — place EVERY module.
"""

FAILURE_SIGNALS = """## Three known failure signals

1. 106 modules carried cross-domain split flags (more than one responsibility).
2. Seven of fourteen economics probes missed Treasury: lane_bandit, lane_quota_cost, route_realized, route_horizon, whole_job, overage, and weekly_cap_guard.
3. Warm-daemons and active-spend had no clean home (neither CLI nor orgsync).
"""


def parse_domain_corpus_arguments() -> argparse.Namespace:
    """Parse caller-supplied locations so the builder is environment-independent."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--seed", type=Path)
    parser.add_argument("--prompt-output", type=Path)
    return parser.parse_args()


def module_name_from_source_path(source_path: Path, source_root: Path) -> str:
    """Return the dotted module name for a Python file below the source root."""
    relative_path = source_path.relative_to(source_root).with_suffix("")
    module_parts = list(relative_path.parts)
    if module_parts[-1] == "__init__":
        module_parts.pop()
    return ".".join(module_parts)


def resolve_relative_import_module(
    current_package: str, imported_module: str | None, level: int
) -> str:
    """Resolve an AST relative import into its absolute dotted module name."""
    package_parts = current_package.split(".")
    retained_parts = package_parts[: len(package_parts) - level + 1]
    if imported_module:
        retained_parts.extend(imported_module.split("."))
    return ".".join(retained_parts)


def internal_import_edges_for_tree(
    syntax_tree: ast.Module, current_package: str, package_name: str
) -> list[str]:
    """Collect unique package-internal module edges from all import statements."""
    internal_edges: set[str] = set()
    package_prefix = f"{package_name}."
    for syntax_node in ast.walk(syntax_tree):
        if isinstance(syntax_node, ast.Import):
            for alias in syntax_node.names:
                if alias.name == package_name or alias.name.startswith(package_prefix):
                    internal_edges.add(alias.name)
        elif isinstance(syntax_node, ast.ImportFrom):
            imported_module = syntax_node.module
            if syntax_node.level:
                imported_module = resolve_relative_import_module(
                    current_package, imported_module, syntax_node.level
                )
            if imported_module and (
                imported_module == package_name
                or imported_module.startswith(package_prefix)
            ):
                internal_edges.add(imported_module)
    return sorted(internal_edges)


def render_module_evidence(source_path: Path, source_root: Path) -> str:
    """Render one module's whole docstring, symbols, and internal imports."""
    source_text = source_path.read_text(encoding="utf-8")
    syntax_tree = ast.parse(source_text, filename=str(source_path))
    relative_path = source_path.relative_to(source_root)
    current_module = module_name_from_source_path(source_path, source_root)
    package_name = current_module.split(".", maxsplit=1)[0]
    current_package = (
        current_module
        if source_path.name == "__init__.py"
        else current_module.rpartition(".")[0]
    )
    module_docstring = ast.get_docstring(syntax_tree, clean=False) or ""
    top_level_symbols = [
        syntax_node.name
        for syntax_node in syntax_tree.body
        if isinstance(syntax_node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
    ]
    internal_edges = internal_import_edges_for_tree(
        syntax_tree, current_package, package_name
    )
    symbols_text = ", ".join(f"`{name}`" for name in top_level_symbols) or "(none)"
    edges_text = ", ".join(f"`{name}`" for name in internal_edges) or "(none)"
    return "\n".join(
        (
            f"## {relative_path.as_posix()}",
            "",
            "### Full module docstring",
            "",
            "```text",
            module_docstring,
            "```",
            "",
            f"### Top-level definitions: {symbols_text}",
            "",
            f"### Internal import edges: {edges_text}",
        )
    )


def build_exhaustive_domain_corpus(source_root: Path, output_path: Path) -> int:
    """Write evidence for every Python module and return the coverage denominator."""
    source_paths = sorted(source_root.rglob("*.py"))
    rendered_modules = [
        render_module_evidence(source_path, source_root)
        for source_path in source_paths
    ]
    emitted_count = len(rendered_modules)
    discovered_count = len(source_paths)
    assert emitted_count == discovered_count, (
        f"coverage mismatch: emitted {emitted_count}, found {discovered_count}"
    )
    coverage_line = f"{emitted_count} modules emitted"
    corpus_text = "\n\n".join(("# Domain discovery evidence corpus", coverage_line, *rendered_modules))
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(f"{corpus_text}\n", encoding="utf-8")
    print(coverage_line)
    return emitted_count


def extract_seed_section(seed_text: str, section_number: int) -> str:
    """Extract a numbered Markdown section using its fixed heading shape."""
    heading_prefix = f"## {section_number}."
    section_start = seed_text.index(heading_prefix)
    following_heading = seed_text.find("\n## ", section_start + len(heading_prefix))
    section_end = len(seed_text) if following_heading == -1 else following_heading
    return seed_text[section_start:section_end].strip()


def build_domain_discovery_prompt(
    corpus_path: Path, seed_path: Path, prompt_output_path: Path
) -> None:
    """Assemble instruction, complete evidence, failure signals, and requested seed sections."""
    corpus_text = corpus_path.read_text(encoding="utf-8").rstrip()
    seed_text = seed_path.read_text(encoding="utf-8")
    seed_domains = extract_seed_section(seed_text, section_number=2)
    seed_failures = extract_seed_section(seed_text, section_number=1)
    prompt_parts = (
        DISCOVERY_INSTRUCTION.rstrip(),
        corpus_text,
        FAILURE_SIGNALS.rstrip(),
        "# Candidate seed taxonomy: domains followed by failure detail",
        seed_domains,
        seed_failures,
    )
    prompt_output_path.parent.mkdir(parents=True, exist_ok=True)
    prompt_output_path.write_text("\n\n".join(prompt_parts) + "\n", encoding="utf-8")


def main() -> None:
    """Run the corpus builder using explicit caller-supplied paths."""
    arguments = parse_domain_corpus_arguments()
    build_exhaustive_domain_corpus(arguments.source_root, arguments.output)
    if (arguments.seed is None) != (arguments.prompt_output is None):
        raise ValueError("--seed and --prompt-output must be supplied together")
    if arguments.seed is not None and arguments.prompt_output is not None:
        build_domain_discovery_prompt(
            arguments.output, arguments.seed, arguments.prompt_output
        )


if __name__ == "__main__":
    main()

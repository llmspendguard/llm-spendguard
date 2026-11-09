"""Shared scope for source-reference checks over maintained documentation."""
from pathlib import Path


HANDWRITTEN_ROOT_DOCUMENT_NAMES = ("README.md", "INTENT.md", "AGENTS.md")
DOCUMENTATION_DIRECTORY_NAME = "docs"
MARKDOWN_GLOB_PATTERN = "**/*.md"
GENERATED_DOCUMENT_RELATIVE_PATHS = frozenset({
    Path("docs") / "CAPABILITY_REFERENCE.md",
})
IGNORED_WORKING_DOCUMENT_RELATIVE_PATHS = frozenset({
    Path("docs") / "DOMAIN_MIGRATION_PLAN_bak_v01.md",
    Path("docs") / "DOMAIN_PHASE4_ACYCLICITY_PLAN_v01.md",
})


def iter_handwritten_document_paths(repository_root):
    """Yield every maintained handwritten Markdown document exactly once."""
    root = Path(repository_root)
    candidates = [root / name for name in HANDWRITTEN_ROOT_DOCUMENT_NAMES]
    candidates.extend((root / DOCUMENTATION_DIRECTORY_NAME).glob(MARKDOWN_GLOB_PATTERN))
    for document_path in sorted(candidates):
        relative_path = document_path.relative_to(root)
        if relative_path in IGNORED_WORKING_DOCUMENT_RELATIVE_PATHS:
            continue
        if relative_path in GENERATED_DOCUMENT_RELATIVE_PATHS:
            continue
        yield document_path

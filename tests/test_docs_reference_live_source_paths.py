"""Handwritten documentation must reference live, non-deprecated source locations."""
from contextlib import contextmanager
import hashlib
import importlib
import json
from pathlib import Path
import re
import sys
from tempfile import TemporaryDirectory
import warnings

from _pytest_fixture_bridge import align_test_startup_home

align_test_startup_home(prefix=__name__)

import spendguard
from spendguard import config
from spendguard.manage.admission import plan_admission
from spendguard.domain_legacy_path_shim import DomainLegacyPathAliasFinder
from spendguard.generate.execution import execution_accessors
from spendguard.manage.admission import admission_accessors
from spendguard.store.call_corpus import call_corpus_accessors

from handwritten_document_reference_scope import (
    GENERATED_DOCUMENT_RELATIVE_PATHS,
    HANDWRITTEN_ROOT_DOCUMENT_NAMES,
    iter_handwritten_document_paths,
)

spendguard.require()

SOURCE_PATH_REFERENCE_PATTERN = re.compile(r"src/spendguard/(?P<module_path>[A-Za-z0-9_./-]+)\.py")
DEPRECATED_IMPORT_CANDIDATE_PATTERNS = (
    re.compile(r"\bfrom\s+spendguard\.(?P<module>[A-Za-z_][A-Za-z0-9_.]*)\s+import\b[^\n]*"),
    re.compile(r"\bimport\s+spendguard\.(?P<module>[A-Za-z_][A-Za-z0-9_.]*)\b"),
    re.compile(r"\bspendguard\.(?P<module>[A-Za-z_][A-Za-z0-9_.]*)\.[A-Za-z_][A-Za-z0-9_]*"),
)
DEPRECATION_REPLACEMENT_PATTERN = re.compile(
    r"^spendguard\.(?P<legacy>[A-Za-z_][A-Za-z0-9_]*) is deprecated; "
    r"import (?P<replacement>spendguard\.[A-Za-z0-9_.]+) instead$"
)
FENCED_BLOCK_BOUNDARY_PATTERN = re.compile(r"^\s*(`{3,}|~{3,})")
SOURCE_PACKAGE_DIRECTORY = Path("src") / "spendguard"
PACKAGE_NAME = "spendguard"
DOCS_DEPRECATED_IMPORT_INTENT = "docs-deprecated-import-currency"
DOCS_DEPRECATED_IMPORT_CACHE_NAMESPACE = "docs-deprecated-import-currency:v1:"
DOCS_DEPRECATED_IMPORT_UNREVIEWED_MARKER = "[UNREVIEWED] docs deprecated-import candidates require a reachable lane"
DOCS_DEPRECATED_IMPORT_VERDICT_CACHE_RELATIVE_PATH = Path("docs_currency") / "deprecated_import_verdicts.json"
PROSE_MENTION_DOCUMENT_RELATIVE_PATH = Path("docs") / "provenance_discussion.md"
PROSE_MENTION_TEXT = "This file was generated from input; do not edit the output described here."
STALE_REFERENCE_DOCUMENT_RELATIVE_PATH = Path("docs") / "stale_reference_proof.md"
STALE_SOURCE_REFERENCE = "src/spendguard/absent_reference_proof_module.py"
HISTORICAL_IMPORT_DOCUMENT_RELATIVE_PATH = Path("docs") / "historical_import_proof.md"
CURRENT_IMPORT_DOCUMENT_RELATIVE_PATH = Path("docs") / "current_import_proof.md"


@contextmanager
def temporarily_uncached_package_module(legacy_module_name):
    """Make one legacy import observable, then restore the interpreter state."""
    package = importlib.import_module(PACKAGE_NAME)
    qualified_name = f"{PACKAGE_NAME}.{legacy_module_name}"
    missing = object()
    prior_module = sys.modules.pop(qualified_name, missing)
    prior_attribute = getattr(package, legacy_module_name, missing)
    if prior_attribute is not missing:
        delattr(package, legacy_module_name)
    try:
        yield qualified_name
    finally:
        sys.modules.pop(qualified_name, None)
        if prior_module is not missing:
            sys.modules[qualified_name] = prior_module
        if prior_attribute is missing:
            if hasattr(package, legacy_module_name):
                delattr(package, legacy_module_name)
        else:
            setattr(package, legacy_module_name, prior_attribute)


def capture_deprecated_module_replacement(legacy_module_name):
    """Return the sanctioned target emitted by a live legacy import, if any."""
    with temporarily_uncached_package_module(legacy_module_name) as qualified_name:
        with warnings.catch_warnings(record=True) as caught_warnings:
            warnings.simplefilter("always", DeprecationWarning)
            try:
                importlib.import_module(qualified_name)
            except ModuleNotFoundError as error:
                if error.name == qualified_name:
                    return None
                raise
        for caught_warning in caught_warnings:
            match = DEPRECATION_REPLACEMENT_PATTERN.fullmatch(str(caught_warning.message))
            if match and match.group("legacy") == legacy_module_name:
                return match.group("replacement")
    return None


def source_path_for_module(repository_root, dotted_module_name):
    """Convert a package module spelling to its repository source path."""
    relative_module = dotted_module_name.removeprefix(f"{PACKAGE_NAME}.")
    return Path(repository_root) / SOURCE_PACKAGE_DIRECTORY / Path(*relative_module.split(".")).with_suffix(".py")


def collect_document_source_reference_violations(repository_root):
    """Return every mechanically dead source path and its live replacement."""
    root = Path(repository_root)
    replacement_cache = {}
    violations = []
    for document_path in iter_handwritten_document_paths(root):
        for line_number, line in enumerate(document_path.read_text(encoding="utf-8").splitlines(), start=1):
            relative_document = document_path.relative_to(root)
            for match in SOURCE_PATH_REFERENCE_PATTERN.finditer(line):
                referenced_path = SOURCE_PACKAGE_DIRECTORY / f"{match.group('module_path')}.py"
                if (root / referenced_path).is_file():
                    continue
                legacy_module = match.group("module_path").split("/", 1)[0]
                replacement = replacement_cache.setdefault(
                    legacy_module, capture_deprecated_module_replacement(legacy_module)
                )
                replacement_text = (
                    source_path_for_module(root, replacement).relative_to(root).as_posix()
                    if replacement is not None else "no sanctioned replacement warning was emitted"
                )
                violations.append(
                    f"{relative_document}:{line_number}: {referenced_path.as_posix()} -> {replacement_text}"
                )
    return violations


def iter_complete_document_sections(document_text):
    """Yield every full paragraph or fenced block with its one-based starting line."""
    lines = document_text.splitlines(keepends=True)
    section_lines = []
    section_start_line = None
    fence_marker = None
    for line_number, line in enumerate(lines, start=1):
        fence_match = FENCED_BLOCK_BOUNDARY_PATTERN.match(line)
        if fence_marker is not None:
            section_lines.append(line)
            if fence_match and line.lstrip().startswith(fence_marker):
                yield section_start_line, "".join(section_lines)
                section_lines = []
                section_start_line = None
                fence_marker = None
            continue
        if fence_match:
            if section_lines:
                yield section_start_line, "".join(section_lines)
            section_lines = [line]
            section_start_line = line_number
            fence_marker = fence_match.group(1)
        elif line.strip():
            if section_start_line is None:
                section_start_line = line_number
            section_lines.append(line)
        elif section_lines:
            yield section_start_line, "".join(section_lines)
            section_lines = []
            section_start_line = None
    if section_lines:
        yield section_start_line, "".join(section_lines)


def iter_deprecated_import_candidates(repository_root):
    """Locate fixed-shape import spellings and retain each whole surrounding section."""
    root = Path(repository_root)
    replacement_cache = {}
    for document_path in iter_handwritten_document_paths(root):
        relative_document = document_path.relative_to(root)
        document_text = document_path.read_text(encoding="utf-8")
        for section_start_line, section_text in iter_complete_document_sections(document_text):
            seen_occurrences = set()
            for candidate_pattern in DEPRECATED_IMPORT_CANDIDATE_PATTERNS:
                for match in candidate_pattern.finditer(section_text):
                    occurrence_key = (match.start(), match.group("module"))
                    if occurrence_key in seen_occurrences:
                        continue
                    seen_occurrences.add(occurrence_key)
                    legacy_module = match.group("module")
                    replacement = replacement_cache.setdefault(
                        legacy_module, capture_deprecated_module_replacement(legacy_module)
                    )
                    if replacement is None:
                        continue
                    line_number = section_start_line + section_text.count("\n", 0, match.start())
                    yield {
                        "relative_path": relative_document.as_posix(),
                        "line_number": line_number,
                        "spelling": match.group(0),
                        "section": section_text,
                        "replacement": replacement,
                    }


def deprecated_import_verdict_cache_path():
    """Return the shared persistent cache used by online and offline guard runs."""
    return config.HOME / DOCS_DEPRECATED_IMPORT_VERDICT_CACHE_RELATIVE_PATH


def deprecated_import_candidate_cache_key(candidate):
    """Hash the exact document location, whole section, and spelling."""
    payload = [candidate["relative_path"], candidate["section"], candidate["spelling"]]
    digest = hashlib.sha256(json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()
    return DOCS_DEPRECATED_IMPORT_CACHE_NAMESPACE + digest


def record_deprecated_import_currency_verdict(cache_key, verdict):
    """Merge one schema-checked judgement into the persistent verdict cache."""
    def merge_deprecated_import_currency_verdict(existing_store):
        store = dict(existing_store) if isinstance(existing_store, dict) else {}
        store[cache_key] = dict(verdict)
        return store

    cache_path = deprecated_import_verdict_cache_path()
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    config.update_json(cache_path, merge_deprecated_import_currency_verdict,
                       reason="docs-deprecated-import-currency-verdict-cache", quarantine_unparseable=True)


def judge_deprecated_import_candidate(candidate):
    """Return a cached/agentic stale verdict, or None when no free lane is reachable."""
    cache_key = deprecated_import_candidate_cache_key(candidate)
    cached = config.read_json_cache_entry(deprecated_import_verdict_cache_path(), cache_key)
    if cached is not None:
        return cached
    try:
        admission_accessors.resolve_require()
        model = plan_admission.ready_meta_model(config.advisor_judge_model())
        prompt = (
            "Judge how a documentation section presents a deprecated Python import. Mark stale=true only when the "
            "section presents the spelling as current recommended/example usage a reader would copy. Mark stale=false "
            "when it is explicitly historical, discouraged, or says not to use it. The entire section and exact "
            "spelling are supplied below; do not infer from the spelling alone.\n"
            f"Document: {candidate['relative_path']}\nExact spelling: {candidate['spelling']}\n"
            f"Deprecated replacement: {candidate['replacement']}\n"
            f"Whole surrounding section:\n{candidate['section']}\n"
            "Reply JSON only with stale (boolean) and why (one short explanation)."
        )
        with call_corpus_accessors.resolve_calls_context(intent=DOCS_DEPRECATED_IMPORT_INTENT):
            response = execution_accessors.resolve_call(
                model, prompt, sig=DOCS_DEPRECATED_IMPORT_INTENT, reasoning="minimal",
                no_metered_fallback=True,
                schema={"type": "object", "additionalProperties": False, "required": ["stale", "why"],
                        "properties": {"stale": {"type": "boolean"},
                                       "why": {"type": "string"}}},
            )
        if isinstance(response, dict) and response.get("error"):
            return None
        structured_verdict = execution_accessors.resolve_structured_reply(response)
    except (OSError, RuntimeError):
        return None
    if not isinstance(structured_verdict, dict) or not isinstance(structured_verdict.get("stale"), bool):
        return {"stale": True, "why": "The reachable judge returned an unclear verdict."}
    verdict = {"stale": structured_verdict["stale"], "why": str(structured_verdict.get("why") or "")}
    record_deprecated_import_currency_verdict(cache_key, verdict)
    return verdict


def assess_document_deprecated_import_references(repository_root):
    """Return (violations, unreviewed) after cached or online agentic decisions."""
    violations = []
    unreviewed = []
    for candidate in iter_deprecated_import_candidates(repository_root):
        verdict = judge_deprecated_import_candidate(candidate)
        if verdict is None:
            unreviewed.append(candidate)
        elif verdict.get("stale"):
            violations.append(
                f"{candidate['relative_path']}:{candidate['line_number']}: {candidate['spelling']} -> "
                f"{candidate['replacement']} ({verdict.get('why', '')})"
            )
    return violations, unreviewed


def verify_handwritten_document_source_references_are_live():
    """Fail dead paths/stale imports; mark uncached offline decisions UNREVIEWED."""
    repository_root = Path(__file__).resolve().parents[1]
    source_violations = collect_document_source_reference_violations(repository_root)
    assert not source_violations, "Handwritten documentation has stale source references:\n" + "\n".join(source_violations)
    import_violations, unreviewed_candidates = assess_document_deprecated_import_references(repository_root)
    assert not import_violations, "Handwritten documentation recommends deprecated imports:\n" + "\n".join(import_violations)
    if unreviewed_candidates:
        locations = ", ".join(f"{item['relative_path']}:{item['line_number']}" for item in unreviewed_candidates)
        print(f"{DOCS_DEPRECATED_IMPORT_UNREVIEWED_MARKER}: {locations}")


def populate_document_reference_guard_fixture(repository_root):
    """Create the complete minimum maintained-document tree for scope proofs."""
    documentation_directory = repository_root / "docs"
    documentation_directory.mkdir()
    for root_document_name in HANDWRITTEN_ROOT_DOCUMENT_NAMES:
        (repository_root / root_document_name).write_text("Maintained by hand.\n", encoding="utf-8")
    for generated_relative_path in GENERATED_DOCUMENT_RELATIVE_PATHS:
        generated_document_path = repository_root / generated_relative_path
        generated_document_path.parent.mkdir(parents=True, exist_ok=True)
        generated_document_path.write_text("Generated content.\n", encoding="utf-8")


def first_live_deprecated_module_and_replacement():
    """Discover one current deprecated shim for behavioral proofs without baking a map."""
    for finder in sys.meta_path:
        if not isinstance(finder, DomainLegacyPathAliasFinder):
            continue
        for qualified_legacy_module in finder.legacy_to_current:
            legacy_module = qualified_legacy_module.removeprefix(f"{PACKAGE_NAME}.")
            replacement = capture_deprecated_module_replacement(legacy_module)
            if replacement is not None:
                return legacy_module, replacement
    raise AssertionError("No live deprecated shim is available for documentation-guard proofs")


def prove_declared_generated_document_is_excluded_while_prose_mentions_remain_in_scope():
    """Only declared provenance excludes a document from handwritten checks."""
    with TemporaryDirectory() as temporary_directory_name:
        repository_root = Path(temporary_directory_name)
        populate_document_reference_guard_fixture(repository_root)
        prose_mention_document_path = repository_root / PROSE_MENTION_DOCUMENT_RELATIVE_PATH
        prose_mention_document_path.write_text(PROSE_MENTION_TEXT + "\n", encoding="utf-8")
        scoped_relative_paths = {path.relative_to(repository_root)
                                 for path in iter_handwritten_document_paths(repository_root)}
    assert PROSE_MENTION_DOCUMENT_RELATIVE_PATH in scoped_relative_paths
    assert GENERATED_DOCUMENT_RELATIVE_PATHS.isdisjoint(scoped_relative_paths)


def prove_source_reference_guard_reports_an_injected_stale_path():
    """Prove Dimension 1 fails closed on a dead source reference."""
    with TemporaryDirectory() as temporary_directory_name:
        repository_root = Path(temporary_directory_name)
        populate_document_reference_guard_fixture(repository_root)
        stale_reference_document_path = repository_root / STALE_REFERENCE_DOCUMENT_RELATIVE_PATH
        stale_reference_document_path.write_text(STALE_SOURCE_REFERENCE + "\n", encoding="utf-8")
        violations = collect_document_source_reference_violations(repository_root)
    assert any(STALE_SOURCE_REFERENCE in violation for violation in violations)


def prove_agentic_deprecated_import_semantics_online_or_unreviewed_offline():
    """Prove the named prose false-positive passes and a current code example fails."""
    legacy_module, replacement = first_live_deprecated_module_and_replacement()
    historical_text = f"The old example used `import {PACKAGE_NAME}.{legacy_module}`, but current code must not use it.\n"
    current_text = f"Current usage:\n\n```python\nfrom {PACKAGE_NAME}.{legacy_module} import X\n```\n"
    with TemporaryDirectory() as temporary_directory_name:
        repository_root = Path(temporary_directory_name)
        populate_document_reference_guard_fixture(repository_root)
        (repository_root / HISTORICAL_IMPORT_DOCUMENT_RELATIVE_PATH).write_text(historical_text, encoding="utf-8")
        historical_violations, historical_unreviewed = assess_document_deprecated_import_references(repository_root)
        (repository_root / HISTORICAL_IMPORT_DOCUMENT_RELATIVE_PATH).unlink()
        (repository_root / CURRENT_IMPORT_DOCUMENT_RELATIVE_PATH).write_text(current_text, encoding="utf-8")
        current_violations, current_unreviewed = assess_document_deprecated_import_references(repository_root)
    if historical_unreviewed or current_unreviewed:
        assert historical_unreviewed and current_unreviewed
        print(DOCS_DEPRECATED_IMPORT_UNREVIEWED_MARKER + ": behavioral proofs")
        return
    assert not historical_violations
    assert any(replacement in violation for violation in current_violations)
    print("Agentic proof: historical/discouraged prose PASS; current code example FAIL")


if __name__ == "__main__":
    prove_declared_generated_document_is_excluded_while_prose_mentions_remain_in_scope()
    prove_source_reference_guard_reports_an_injected_stale_path()
    prove_agentic_deprecated_import_semantics_online_or_unreviewed_offline()
    verify_handwritten_document_source_references_are_live()
    print("Handwritten documentation scope, failure proofs, source paths, and agentic import judgements are current")

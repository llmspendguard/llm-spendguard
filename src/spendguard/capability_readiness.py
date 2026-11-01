"""Fail-closed readiness assessment; absence of evidence is never success.

The manifest is the versioned declaration rendered into docs/READINESS.md.
Gate implementations return evidence, not a truthy placeholder or inferred pass.
"""
from dataclasses import asdict, dataclass
from importlib import import_module
from importlib.resources import files
import json
from pathlib import Path
from typing import Any, Literal

READINESS_MANIFEST_RESOURCE = "capability_readiness_manifest.json"
READINESS_SUCCESS_EXIT = 0
READINESS_INCOMPLETE_EXIT = 1
READINESS_REPORT_STATES = ("PASS", "FAIL", "UNREVIEWED")


@dataclass(frozen=True)
class CapabilityReadinessEvidence:
    status: Literal["PASS", "FAIL", "UNREVIEWED"]
    detail: str
    evidence: tuple[str, ...] = ()


@dataclass(frozen=True)
class CapabilityReadinessItem:
    id: str
    description: str
    gate: str | None
    arguments: dict[str, Any]
    reason: str
    standard: str | None = None
    section: str | None = None


@dataclass(frozen=True)
class CapabilityReadinessOutcome:
    id: str
    description: str
    status: Literal["PASS", "FAIL", "UNREVIEWED"]
    detail: str
    evidence: tuple[str, ...]


def load_capability_readiness_manifest():
    """Load the packaged declaration; a malformed manifest is a readiness failure."""
    declaration = json.loads(files(__package__).joinpath(READINESS_MANIFEST_RESOURCE).read_text(encoding="utf-8"))
    items = tuple(CapabilityReadinessItem(
        id=item["id"], description=item["description"], gate=item.get("gate"),
        arguments=item.get("arguments", {}), reason=item.get("reason", ""),
        standard=item.get("standard"),
        section=item.get("section"),
    ) for item in declaration["items"])
    if len({item.id for item in items}) != len(items):
        raise ValueError("Duplicate readiness item identifiers")
    if not items:
        raise ValueError("An empty readiness manifest cannot establish readiness")
    return declaration["revision"], items


def assess_capability_readiness(*, repo_root=None, environment_report=None):
    """Run every declared gate; unavailable evidence remains explicitly UNREVIEWED."""
    revision, items = load_capability_readiness_manifest()
    context = {
        "repo_root": Path.cwd() if repo_root is None else Path(repo_root),
        "environment_report": environment_report,
    }
    outcomes = []
    for item in items:
        if item.gate is None:
            result = CapabilityReadinessEvidence("UNREVIEWED", item.reason or "No gate has been reviewed.")
        else:
            try:
                module_name, function_name = item.gate.split(":", 1)
                gate_function = getattr(import_module(module_name), function_name)
                result = gate_function(context=context, **item.arguments)
                if not isinstance(result, CapabilityReadinessEvidence):
                    raise TypeError("Readiness gates must return typed evidence")
                if result.status not in READINESS_REPORT_STATES:
                    raise ValueError(f"Invalid readiness status: {result.status}")
                if result.status == "PASS" and not result.evidence:
                    raise ValueError("PASS requires explicit gate evidence")
            except (ImportError, FileNotFoundError) as error:
                result = CapabilityReadinessEvidence("UNREVIEWED", f"Gate unavailable: {type(error).__name__}: {error}")
            except Exception as error:
                result = CapabilityReadinessEvidence("FAIL", f"Gate failed: {type(error).__name__}: {error}")
        outcomes.append(CapabilityReadinessOutcome(item.id, item.description, result.status, result.detail, result.evidence))
    covered = sum(outcome.status != "UNREVIEWED" for outcome in outcomes)
    passed = sum(outcome.status == "PASS" for outcome in outcomes)
    return {
        "revision": revision,
        "coverage": {"covered": covered, "total": len(outcomes), "passed": passed},
        "ready": passed == len(outcomes),
        "items": [asdict(outcome) for outcome in outcomes],
    }


def render_capability_readiness_report(report):
    """Render each result and the two distinct counts: coverage and passing gates."""
    lines = [f"Readiness revision {report['revision']}"]
    for item in report["items"]:
        lines.append(f"{item['status']} {item['id']}: {item['detail']}")
    coverage = report["coverage"]
    lines.append(f"Coverage {coverage['covered']}/{coverage['total']}; passing {coverage['passed']}/{coverage['total']}")
    return "\n".join(lines)


def capability_selfcheck_exit_code(report, tolerate_unreviewed=()):
    """Return success only when every outcome passes or is explicitly tolerated UNREVIEWED."""
    tolerated_identifiers = frozenset(tolerate_unreviewed)
    acceptable_states = (
        item["status"] == "PASS"
        or (item["status"] == "UNREVIEWED" and item["id"] in tolerated_identifiers)
        for item in report["items"]
    )
    return READINESS_SUCCESS_EXIT if all(acceptable_states) else READINESS_INCOMPLETE_EXIT


def render_capability_readiness_definition():
    """Derive the versioned readiness checklist directly from its manifest."""
    revision, items = load_capability_readiness_manifest()
    lines = ["# Readiness", "", f"Definition revision: {revision}", "",
             "Square means every checklist item has a reviewed deterministic gate and every gate passes.",
             "`spendguard selfcheck` runs each gate and exits nonzero for FAIL or UNREVIEWED.",
             "Coverage counts PASS and FAIL items with reviewed gates; UNREVIEWED counts as neither coverage nor success.",
             "A supplied clean-environment report must match the current input fingerprint and its complete verification logs.",
             "", "## Project invariants", ""]
    for item in items:
        if item.standard is not None or item.section is not None:
            continue
        lines.extend([f"- [ ] **{item.id}** — {item.description}",
                      f"  Gate: `{item.gate}`; inputs: `{json.dumps(item.arguments, sort_keys=True)}`.", ""])
    lines.extend(["## External standards — reviewed offline subsets", "",
                  "PASS covers the declared offline subset, not certification or an external badge. Reviewed N/A checks remain tracked below.", ""])
    for item in items:
        if item.standard is not None:
            lines.extend([f"- [ ] [{item.description}]({item.standard}) — {item.reason}",
                          f"  Gate: `{item.gate}`."])
            for check in item.arguments.get("reviews", []):
                lines.append(f"  - **{check['id']}** ({check['disposition']}): {check['reason']} Tracking: {check['tracking']}.")
            lines.append("")
    lines.extend(["## Architectural & code quality", "",
                  "Objective offline fitness functions enforce the recorded scope and measured ratchets. Reviewed exclusions are tracked explicitly.", ""])
    for item in items:
        if item.section != "architecture":
            continue
        lines.extend([f"- [ ] **{item.id}** — {item.description}",
                      f"  Gate: `{item.gate}`; inputs: `{json.dumps(item.arguments, sort_keys=True)}`.",
                      f"  Review: {item.reason}", ""])
    return "\n".join(lines)


def run_capability_selfcheck(*, repo_root=None, environment_report=None, json_output=False, tolerate_unreviewed=()):
    """Print readiness; explicit named UNREVIEWED tolerances affect only the process exit."""
    try:
        report = assess_capability_readiness(repo_root=repo_root, environment_report=environment_report)
    except Exception as error:
        report = {
            "revision": None, "coverage": {"covered": 0, "total": None, "passed": 0},
            "ready": False,
            "items": [{"id": "manifest", "description": "Readiness manifest", "status": "FAIL", "detail": f"{type(error).__name__}: {error}", "evidence": []}],
        }
    print(json.dumps(report, indent=2) if json_output else render_capability_readiness_report(report))
    return capability_selfcheck_exit_code(report, tolerate_unreviewed)

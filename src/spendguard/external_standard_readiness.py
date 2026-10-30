"""Offline evidence for explicitly reviewed subsets; never external certification."""
import hashlib
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import tomllib
import zipfile
from email.parser import BytesParser
from urllib.parse import unquote, urlsplit

from .capability_readiness import CapabilityReadinessEvidence

EXTERNAL_STANDARD_TIMEOUT_SECONDS = 300
EXTERNAL_STANDARD_COMMAND_SUCCESS = 0
EXTERNAL_STANDARD_SHA_PATTERN = r"[0-9a-f]{40}"
EXTERNAL_STANDARD_EXPRESSION_PATTERN = r"\$\{\{(.*?)\}\}"
EXTERNAL_STANDARD_ESCAPE = "\x1b["
EXTERNAL_STANDARD_MARKDOWN_LINK_PATTERN = r"\[[^\]\n]+\]\(([^\s)]+)\)"
EXTERNAL_STANDARD_MARKDOWN_HEADING_PATTERN = r"^#{1,6}\s+(.+?)\s*#*\s*$"


def run_external_standard_command(arguments, *, cwd, environment=None):
    """Retain complete subprocess evidence and refuse any nonzero exit."""
    completed = subprocess.run(arguments, cwd=cwd, env=environment, capture_output=True,
                               text=True, timeout=EXTERNAL_STANDARD_TIMEOUT_SECONDS, check=False)
    if completed.returncode != EXTERNAL_STANDARD_COMMAND_SUCCESS:
        raise ValueError(f"{arguments}: exit {completed.returncode}\n{completed.stdout}\n{completed.stderr}")
    return completed


def assess_external_standard_subset(*, context, kind, reviews, policy):
    """Validate review records, then execute only their declared deterministic contract."""
    if not reviews or len({review['id'] for review in reviews}) != len(reviews):
        raise ValueError("Missing or duplicate sub-check reviews")
    if not any(review['disposition'] == 'GATED-OFFLINE' for review in reviews):
        raise ValueError("No gated offline subset declared")
    for review in reviews:
        if review["disposition"] not in ("GATED-OFFLINE", "REVIEWED-N/A", "REVIEWED-EXCEPTION"):
            raise ValueError(f"Unreviewed sub-check: {review}")
        if not review.get("reason") or not review.get("tracking"):
            raise ValueError(f"Incomplete sub-check review: {review}")
    root = Path(context["repo_root"])
    evidence = []
    for relative in policy.get("required_files", []):
        path = root / relative
        if not path.is_file() or not path.read_bytes():
            raise ValueError(f"Missing/empty required evidence: {relative}")
        evidence.append(relative)
    if kind == "diataxis":
        for quadrant, mapping in policy["quadrants"].items():
            document = root / mapping["document"]
            if not document.is_file():
                raise ValueError(f"Missing {quadrant}: {document}")
            discoverable = False
            for index in mapping['indexes']:
                index_path = root / index
                for target in re.findall(EXTERNAL_STANDARD_MARKDOWN_LINK_PATTERN, index_path.read_text()):
                    parsed = urlsplit(target)
                    if parsed.scheme or parsed.netloc or target != mapping['link']:
                        continue
                    resolved = (index_path.parent / unquote(parsed.path)).resolve() if parsed.path else index_path.resolve()
                    if resolved == document.resolve():
                        discoverable = True
            if not discoverable:
                raise ValueError(f"Undiscoverable {quadrant}: {document}")
            heading = mapping.get('heading')
            if heading and heading not in re.findall(EXTERNAL_STANDARD_MARKDOWN_HEADING_PATTERN, document.read_text(), re.MULTILINE):
                raise ValueError(f"Missing {quadrant} target heading: {heading}")
            evidence.append(f"{quadrant}: {mapping['document']}")
    elif kind == "best-practices":
        run_external_standard_command(["git", "rev-parse", "--is-inside-work-tree"], cwd=root)
        project = tomllib.loads((root / policy["project"]).read_text())
        if policy["analysis_tool"] not in project["tool"]:
            raise ValueError("Static analysis tool not configured")
        import yaml
        workflow = yaml.safe_load((root / policy["sast_workflow"]).read_text())
        actions = [step.get("uses", "").split("@", 1)[0] for job in workflow["jobs"].values() for step in job.get("steps", [])]
        if not set(policy["sast_actions"]).issubset(actions):
            raise ValueError("SAST actions missing")
        evidence.append("git repository; configured static analysis and automated suite")
    elif kind == "scorecard":
        import yaml
        workflows = sorted({path for pattern in policy["workflows"] for path in root.glob(pattern)})
        if not workflows:
            raise ValueError("No workflows available for review")
        for relative in workflows:
            workflow = yaml.safe_load(relative.read_text())
            permissions = workflow.get("permissions")
            if not isinstance(permissions, dict) or permissions.get("contents") != "read" or any(value != "read" for value in permissions.values()):
                raise ValueError(f"Workflow-wide permissions exceed read or absent: {relative}")
            triggers = workflow.get("on", workflow.get(True, {}))
            if "pull_request_target" in triggers:
                raise ValueError(f"Privileged pull request event prohibited: {relative}")
            for job in workflow["jobs"].values():
                reusable = job.get("uses")
                if reusable and not reusable.startswith("./") and not re.fullmatch(EXTERNAL_STANDARD_SHA_PATTERN, reusable.rsplit("@", 1)[-1]):
                    raise ValueError(f"Reusable workflow not SHA pinned: {reusable}")
                for step in job.get("steps", []):
                    action = step.get("uses")
                    if action and not action.startswith("./") and not re.fullmatch(EXTERNAL_STANDARD_SHA_PATTERN, action.rsplit("@", 1)[-1]):
                        raise ValueError(f"Action not SHA pinned: {action}")
                    for expression in re.findall(EXTERNAL_STANDARD_EXPRESSION_PATTERN, step.get("run", ""), re.DOTALL):
                        if expression.strip() not in policy["safe_run_expressions"]:
                            raise ValueError(f"Unreviewed inline shell expression: {expression}")
            evidence.append(str(relative.relative_to(root)))
        tracked = run_external_standard_command(["git", "ls-files", "-z"], cwd=root).stdout.split("\0")
        artifacts = {name for name in tracked if Path(name).suffix in policy["binary_suffixes"]}
        waivers = {waiver["path"]: waiver for waiver in policy["binary_waivers"]}
        if artifacts != set(waivers):
            raise ValueError(f"Unwaived/missing binary artifacts: {artifacts.symmetric_difference(waivers)}")
        for relative, waiver in waivers.items():
            digest = hashlib.sha256((root / relative).read_bytes()).hexdigest()
            if digest != waiver["sha256"] or digest not in (root / waiver["document"]).read_text():
                raise ValueError(f"Binary waiver mismatch: {relative}")
            with zipfile.ZipFile(root / relative) as archive:
                records = [name for name in archive.namelist()
                           if name.endswith(policy["waiver_metadata_suffix"]) and len(Path(name).parts) == policy["wheel_metadata_depth"]]
                if len(records) != 1 or BytesParser().parsebytes(archive.read(records[0]))['Version'] != waiver['version']:
                    raise ValueError(f"Binary waiver version mismatch: {relative}")
            evidence.append(f"REVIEWED EXCEPTION {relative}: sha256 {digest}; {waiver['document']}")
    elif kind == "packaging":
        evidence.extend(verify_external_packaging_artifacts(root, policy))
    elif kind == "cli":
        from ._version import __version__
        environment = os.environ.copy()
        environment.update(policy["environment"])
        for arguments in policy["help_arguments"]:
            result = run_external_standard_command([sys.executable, "-m", "spendguard", *arguments], cwd=root, environment=environment)
            if not all(group in result.stdout for group in policy["help_groups"]) or EXTERNAL_STANDARD_ESCAPE in result.stdout:
                raise ValueError(f"Ungrouped/colored help: {arguments}")
            evidence.append(f"help {arguments}: exit 0, grouped, no ANSI")
        for arguments in policy["version_arguments"]:
            result = run_external_standard_command([sys.executable, "-m", "spendguard", *arguments], cwd=root, environment=environment)
            if __version__ not in result.stdout:
                raise ValueError("Version path does not print installed version")
        invalid = subprocess.run([sys.executable, "-m", "spendguard", policy["unknown_command"]], cwd=root,
                                 env=environment, capture_output=True, text=True, timeout=EXTERNAL_STANDARD_TIMEOUT_SECONDS, check=False)
        if not invalid.returncode or invalid.stdout or not invalid.stderr or policy["suggestion"] not in invalid.stderr:
            raise ValueError(f"Unknown command lacks stderr suggestion: {invalid}")
        from .capability_readiness_gates import run_capability_script_readiness_gate
        help_result = run_capability_script_readiness_gate(context=context, scripts=policy["help_scripts"])
        if help_result.status != "PASS":
            return help_result
        evidence.extend(help_result.evidence)
    else:
        raise ValueError(f"Unknown offline standard subset: {kind}")
    if not evidence:
        raise ValueError("No offline evidence")
    return CapabilityReadinessEvidence("PASS", "Reviewed offline subset passed; external certification is not claimed", tuple(evidence))


def verify_external_packaging_artifacts(root, policy):
    """Build both artifacts offline and import the wheel in a fresh isolated venv."""
    from .distribution import distribution_policy, ensure_distribution_build_backend, ensure_distribution_build_pip
    settings = distribution_policy()
    with tempfile.TemporaryDirectory() as temporary:
        workspace = Path(temporary)
        checkout = workspace / policy["source_directory"]
        checkout.mkdir()
        tracked = run_external_standard_command(["git", "ls-files", "-z"], cwd=root).stdout.split("\0")
        for relative in tracked:
            if not relative or not (root / relative).is_file():
                continue
            destination = checkout / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(root / relative, destination)
        ensure_distribution_build_pip(settings)
        ensure_distribution_build_backend(checkout, settings)
        environment = os.environ.copy()
        environment.update(policy["environment"])
        artifacts = workspace / policy["artifact_directory"]
        run_external_standard_command([sys.executable, "-m", "build", "--no-isolation", "--sdist", "--wheel", "--outdir", str(artifacts), str(checkout)], cwd=workspace, environment=environment)
        built = sorted(artifacts.iterdir())
        if {path.suffix for path in built} != set(policy["artifact_suffixes"]) or len(built) != len(policy["artifact_suffixes"]):
            raise ValueError("Build did not produce exactly sdist and wheel")
        run_external_standard_command([sys.executable, "-m", "twine", "check", "--strict", *map(str, built)], cwd=workspace, environment=environment)
        wheel = next(path for path in built if path.suffix == policy["wheel_suffix"])
        with zipfile.ZipFile(wheel) as archive:
            metadata_names = [name for name in archive.namelist() if name.endswith(policy["metadata_suffix"])]
            if len(metadata_names) != 1:
                raise ValueError("Wheel metadata not unique")
            metadata = BytesParser().parsebytes(archive.read(metadata_names[0]))
            for field in policy["metadata_fields"]:
                if not metadata.get(field):
                    raise ValueError(f"Missing core metadata: {field}")
            if not metadata.get_payload().strip():
                raise ValueError("Missing long description/readme")
        venv = workspace / policy["venv_directory"]
        run_external_standard_command([sys.executable, "-m", "venv", str(venv)], cwd=workspace, environment=environment)
        interpreter = venv / policy["venv_python"]
        run_external_standard_command([str(interpreter), "-m", "pip", "install", "--no-index", "--no-deps", str(wheel)], cwd=workspace, environment=environment)
        # Copy the installed runtime distribution offline from the verification interpreter.
        import importlib.metadata
        dependency = importlib.metadata.distribution(policy["runtime_dependency"])
        site = run_external_standard_command([str(interpreter), "-c", policy["site_probe"]], cwd=workspace, environment=environment).stdout.strip()
        for entry in dependency.files or ():
            source = Path(dependency.locate_file(entry))
            destination = Path(site) / entry
            if source.is_file() and destination.resolve().is_relative_to(Path(site).resolve()):
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, destination)
        environment.pop("PYTHONPATH", None)
        run_external_standard_command([str(interpreter), "-I", "-c", policy["import_probe"]], cwd=workspace, environment=environment)
        result = run_external_standard_command([str(venv / policy["venv_command"]), "version"], cwd=workspace, environment=environment)
        if metadata["Version"] not in result.stdout:
            raise ValueError("Installed CLI version differs from wheel metadata")
        return ["offline sdist + wheel; twine check --strict", "required core metadata; clean-venv wheel import and CLI version"]
